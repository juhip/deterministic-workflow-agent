"""Optional: the model rewrites the messages people act on, in plain language.

Code has already made every decision and written each message from a template, with exact
numbers. The model may change how a message reads, never what it says. Each rewrite is checked:

  no invented facts   every number, amount, date, ID, policy citation and name in the rewrite
                      must appear in the original
  nothing dropped     every number, amount, date, production order number (in whatever format
                      the data uses), part, supplier, customer and person in the original must
                      still be there (only policy citations such as
                      "§6" and list numbering may be left out)

A rewrite that fails either check is discarded and the original template is kept. If the model is
missing, slow or returns nothing usable, every message stays as written. Order rationales are not
rewritten: they stay as the exact audit record of each decision.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .alerts import Alert
from .llm import LLMError, ModelProvider
from .models import ScenarioState
from .release import approvers_by_authority, conflict_decider
from .rules import Rulebook

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "message_writer.md"
REWRITE = ("ACTION", "CRITICAL")                     # the messages someone has to act on

MONEY = re.compile(r"\$\d[\d,]*(?:\.\d+)?")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
IDS = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)+\b")            # WO-7102, AGT-0011, PT-103, MEMO-2026-018, HF-PUR-100
CITATION = re.compile(r"§\s?[\d.]+(?:,\s?[\d.]+)*")          # "§7, 7.1": may be dropped, never invented
LIST_MARK = re.compile(r"\(\d+\)\s")                          # "(1) " list numbering in the templates
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _facts(text: str, known: set[str]) -> dict[str, set[str]]:
    plain = LIST_MARK.sub(" ", CITATION.sub(" ", IDS.sub(" ", text)))
    return {"ids": set(IDS.findall(text)), "citations": set(CITATION.findall(text)),
            "numbers": {n.replace(",", "") for n in NUMBER.findall(plain)},
            "money": set(MONEY.findall(text)), "dates": set(DATE.findall(text)),
            "names": {n for n in known if n in text}}


def check_invented(source: str, text: str, known_names: set[str]) -> str | None:
    """None if every number, amount, date, ID, citation and name in `text` also appears in `source`."""
    o, r = _facts(source, known_names), _facts(text, known_names)
    invented = set()
    for kind in ("ids", "citations", "numbers", "money", "dates", "names"):
        invented |= r[kind] - o[kind]
    return f"added {', '.join(sorted(invented))}" if invented else None


def check(original: str, rewrite: str, known_names: set[str]) -> str | None:
    """None if the rewrite keeps the facts; otherwise why it was rejected."""
    problem = check_invented(original, rewrite, known_names)
    if problem:
        return problem
    o, r = _facts(original, known_names), _facts(rewrite, known_names)
    dropped = set()
    for kind in ("numbers", "money", "dates", "names"):
        dropped |= o[kind] - r[kind]
    if dropped:
        return f"dropped {', '.join(sorted(dropped))}"
    return None


def reader_for(alert: Alert, rules: Rulebook) -> str:
    m = re.match(r"Approval needed from (.+?) for ", alert.text)
    if m:
        return m.group(1)
    if alert.text.startswith("Decision needed"):
        return conflict_decider(rules)
    if alert.text.startswith("Cannot meet"):
        return "the procurement planner and the customer-facing team"
    return "the procurement planner"


def known_names(state: ScenarioState, rules: Rulebook) -> set[str]:
    names = {s.name for s in state.suppliers.values()} | {o.customer for o in state.schedule}
    names |= {o.order_id for o in state.schedule}          # production order numbers, in whatever format the data uses
    names |= {c.name for c in state.components.values()}
    names |= {p.get("name", "") for p in state.products.values()}
    names |= set(approvers_by_authority(rules, state.current_date))
    names |= {rules.raw.get("settings", {}).get(k, "") for k in ("receiving_team",)}
    return {n for n in names if n and len(n) > 2}


def rewrite_alerts(client: ModelProvider, state: ScenarioState, rules: Rulebook,
                   alerts: list[Alert]) -> tuple[list[Alert], Alert]:
    """Returns the alerts (rewritten where the check passed) and one INFO alert saying what happened."""
    picked = [(f"M{i + 1}", a) for i, a in enumerate(alerts) if a.severity in REWRITE]
    if not picked:
        return alerts, Alert("INFO", "AI message writer: no messages needed rewriting.")
    payload = [{"id": mid, "reader": reader_for(a, rules), "text": a.text} for mid, a in picked]
    try:
        raw = client.chat([{"role": "system", "content": PROMPT.read_text()},
                           {"role": "user", "content": "MESSAGES\n" + json.dumps(payload, indent=1)}])
        m = re.search(r"\{.*\}", raw, re.S)
        reply = json.loads(m.group(0)) if m else None
    except (LLMError, ValueError) as e:
        return alerts, Alert("INFO", f"AI message writer skipped ({str(e)[:100]}); messages are as the agent wrote them.")
    if not isinstance(reply, dict) or not isinstance(reply.get("messages"), list):
        return alerts, Alert("INFO", "AI message writer returned an unusable answer; messages are as the agent wrote them.")

    by_id = {str(r.get("id")): str(r.get("text", "")).strip() for r in reply["messages"] if isinstance(r, dict)}
    names = known_names(state, rules)
    out, done, kept = list(alerts), 0, []
    for mid, alert in picked:
        text = by_id.get(mid)
        if not text:
            continue
        problem = check(alert.text, text, names)
        if problem:
            kept.append(f"{mid} ({problem})")
            continue
        out[out.index(alert)] = Alert(alert.severity, f"{text} (Plain-language version by {client.model_name}; "
                                                      "every figure checked against the agent's plan.)")
        done += 1
    note = (f"AI message writer ({client.model_name}) rewrote {done} of {len(picked)} message(s) for the people who act "
            f"on them" + (f"; kept the original for {len(kept)} that failed the fact check: {'; '.join(kept)}" if kept else "")
            + ". Order rationales are never rewritten.")
    return out, Alert("INFO", note)
