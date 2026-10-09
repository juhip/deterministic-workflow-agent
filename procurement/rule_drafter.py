"""The AI rule drafter: turns a new management memo into proposed rulebook changes.

Flow
  1. Read the memo (PDF, or plain text).
  2. Ask the open model to propose changes, giving it the current rulebook as examples.
  3. Check every proposed change in code. A change is rejected if:
       - its rule type or selector is not one the agent understands,
       - its quote does not appear word for word in the memo (no invented policy),
       - it cites a different document than the memo,
       - it modifies or ends a rule that does not exist, or adds an id that already exists,
       - the resulting rulebook fails to load.
  4. Show a person the accepted changes, the rejected ones with reasons, the model's open
     questions, and (optionally) how the plan for a scenario would change.
  5. Nothing changes until a person re-runs with --apply.

The model proposes; code verifies; a person approves.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMError, ModelProvider
from .rules import KNOWN_TYPES, SELECTOR_KEYS, Rulebook, RulebookError

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "rule_drafter_system.md"
MEMO_ID = re.compile(r"MEMO-\d{4}-\d{3}")


def read_memo(path: str | Path) -> str:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader  # optional dependency
        except ImportError as e:
            raise RuntimeError("reading PDF memos needs pypdf (pip install pypdf), or pass the memo as .txt") from e
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    return path.read_text()


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def quote_in_memo(quote: str, memo: str) -> bool:
    """Every fragment of the quote (split on '...') must appear in the memo, ignoring spacing and case."""
    memo_n = _norm(memo)
    parts = [p for p in re.split(r"\.\.\.|…", quote) if p.strip()]
    return bool(parts) and all(_norm(p).strip(" .,;") in memo_n for p in parts)


@dataclass
class Proposal:
    document: dict
    summary: str
    accepted: list[dict] = field(default_factory=list)
    rejected: list[tuple[dict, str]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    new_rulebook: dict | None = None


def ask_model(client: ModelProvider, memo: str, rulebook: Rulebook) -> dict:
    allowed = (f"ALLOWED RULE TYPES: {', '.join(sorted(KNOWN_TYPES))}\n"
               f"SELECTOR KEYS: {', '.join(sorted(SELECTOR_KEYS))}")
    current = {k: v for k, v in rulebook.raw.items() if k != "settings"}   # settings are business choices, not policy
    messages = [{"role": "system", "content": PROMPT_PATH.read_text()},
                {"role": "user", "content": f"NEW MEMO\n{memo}\n\nCURRENT RULEBOOK\n{json.dumps(current, indent=1)}\n\n{allowed}"}]
    raw = client.chat(messages)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise LLMError("the model did not return JSON")
    try:
        return json.loads(match.group(0))
    except ValueError as e:
        raise LLMError(f"the model returned invalid JSON: {e}") from e


def check(reply: dict, memo: str, rulebook: Rulebook) -> Proposal:
    doc = reply.get("document") or {}
    memo_ids = set(MEMO_ID.findall(memo))
    prop = Proposal(doc, str(reply.get("summary", "")), questions=[str(q) for q in reply.get("questions", [])])
    if doc.get("id") not in memo_ids:
        prop.rejected.append(({"document": doc}, f"document id {doc.get('id')!r} does not appear in the memo "
                                                 f"(memo mentions: {', '.join(sorted(memo_ids)) or 'no memo id'})"))
        return prop

    raw = copy.deepcopy(rulebook.raw)
    ids = {r["id"] for r in raw["rules"]}
    for ch in reply.get("changes", []) or []:
        action = ch.get("action")
        if action == "add":
            rule = ch.get("rule") or {}
            why = None
            if rule.get("id") in ids:
                why = f"rule id {rule.get('id')!r} already exists"
            elif rule.get("type") not in KNOWN_TYPES:
                why = f"rule type {rule.get('type')!r} is not one the agent understands"
            elif (rule.get("source") or {}).get("document") != doc["id"]:
                why = "the rule must cite the memo as its source"
            elif not quote_in_memo(str((rule.get("source") or {}).get("quote", "")), memo):
                why = "its quote does not appear in the memo"
            elif rule.get("overrides") and rule["overrides"] not in ids:
                why = f"it overrides unknown rule {rule['overrides']!r}"
            if why:
                prop.rejected.append((ch, why))
                continue
            trial = copy.deepcopy(raw)
            trial["rules"].append(rule)
        elif action == "end":
            rid, end = ch.get("rule_id"), ch.get("end_date")
            if rid not in ids:
                prop.rejected.append((ch, f"rule {rid!r} does not exist"))
                continue
            trial = copy.deepcopy(raw)
            next(r for r in trial["rules"] if r["id"] == rid)["effective_to"] = end
        else:
            prop.rejected.append((ch, f"unknown action {action!r}"))
            continue
        try:
            Rulebook(trial)                          # the same checks the agent runs on load
        except RulebookError as e:
            prop.rejected.append((ch, f"the rulebook would not load: {e}"))
            continue
        raw = trial
        ids = {r["id"] for r in raw["rules"]}
        prop.accepted.append(ch)

    if prop.accepted:
        if doc["id"] not in {d["id"] for d in raw.get("documents", [])}:
            raw.setdefault("documents", []).append({"id": doc["id"], "file": doc.get("file", ""), "date": doc.get("date", "")})
        raw["status"] = "proposed - awaiting human review"
        prop.new_rulebook = raw
    return prop


def describe(prop: Proposal) -> str:
    lines = [f"Memo {prop.document.get('id', '?')}: {prop.summary}", ""]
    if prop.accepted:
        lines.append("PROPOSED CHANGES (passed every check; a person must approve them):")
        for ch in prop.accepted:
            if ch["action"] == "add":
                r = ch["rule"]
                to = f" to {r.get('effective_to')}" if r.get("effective_to") else ""
                ov = f", replacing '{r['overrides']}'" if r.get("overrides") else ""
                lines.append(f"  + ADD '{r['id']}' ({r['type']}{ov}), effective {r['effective_from']}{to}")
                lines.append(f"      applies to: {r['scope'].get('description', r['scope'].get('selector'))}")
                lines.append(f"      settings:   {json.dumps(r.get('params', {}))}")
                lines.append(f"      quote:      \"{r['source'].get('quote', '')}\"")
            else:
                lines.append(f"  - END '{ch['rule_id']}' on {ch['end_date']}")
            if ch.get("reason"):
                lines.append(f"      reason:     {ch['reason']}")
    else:
        lines.append("No changes passed the checks.")
    if prop.rejected:
        lines += ["", "REJECTED (not applied):"]
        for ch, why in prop.rejected:
            label = (ch.get("rule") or {}).get("id") or ch.get("rule_id") or ch.get("document", {}).get("id", "?")
            lines.append(f"  x {label}: {why}")
    if prop.questions:
        lines += ["", "QUESTIONS FOR A PERSON:"] + [f"  ? {q}" for q in prop.questions]
    return "\n".join(lines)


def impact(state_loader, scenario: str, current: Rulebook, proposed: Rulebook) -> str:
    """How the plan for one scenario would change under the proposed rulebook (a dry run)."""
    from .pipeline import run
    before = {(p.component_id, p.supplier_id, p.mode): p.quantity for p in run(state_loader(scenario), current).pos}
    after = {(p.component_id, p.supplier_id, p.mode): p.quantity for p in run(state_loader(scenario), proposed).pos}
    if before == after:
        return f"Impact on {Path(scenario).name}: no change to any purchase order."
    out = [f"Impact on {Path(scenario).name}:"]
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b != a:
            out.append(f"  {k[0]} from {k[1]}: {b or 0} -> {a or 0} units")
    return "\n".join(out)


def unknown_memo_files(policy_dir: str | Path, rulebook: Rulebook) -> list[str]:
    """Memo files in the policy folder that the rulebook does not list yet."""
    known = {d.get("file") for d in rulebook.raw.get("documents", [])}
    d = Path(policy_dir)
    if not d.is_dir():
        return []
    return sorted(f.name for f in d.iterdir() if f.name.lower().startswith("memo") and f.name not in known)
