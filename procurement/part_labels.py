"""Understanding the parts: the model reads every part and says which policy rules apply to it.

This is where held-out data differs most: a new part name the rulebook's keywords never saw.
So the model is the primary reader of part names and descriptions, and its answers feed the
same rule checks as everything else (supplier eligibility, the magnet split, the circuit-board
freeze, receiving documents, critical-part thresholds).

Guardrails:
  limited vocabulary  a part can only be given the policy's own critical categories and the ids
                      of rules whose scope is written in words (memos, certificates); anything
                      else is ignored
  safety floor        the rulebook's keyword rules still apply: the agent uses the union, so the
                      model can add a rule to a part but never remove one. Disagreements in
                      either direction are reported for a person to confirm
  held until checked  a part classified by the model alone has its orders held for review
  repeatable          verified answers are cached per part, rulebook and model, so a rerun sees
                      the same labels
  fails safe          no model or no usable answer: the keyword rules alone, and the run says so
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .alerts import Alert
from .llm import LLMError, ModelProvider
from .models import Component, ScenarioState
from .rules import Rule, Rulebook, matches

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "part_classifier.md"
WORDED = {"any_keywords", "name_keywords", "all_of", "id_aliases"}   # scopes written in words, not data fields


@dataclass
class PartLabels:
    critical: dict[str, str] = field(default_factory=dict)       # part -> policy critical label (model)
    scope: dict[str, set[str]] = field(default_factory=dict)     # part -> rule ids (model)
    holds: dict[str, list[str]] = field(default_factory=dict)    # part -> reasons to hold its orders
    alerts: list[Alert] = field(default_factory=list)


def worded_rules(rules: Rulebook, today) -> list[Rule]:
    return [r for r in rules.rules if r.active_on(today) and WORDED & set(r.scope.get("selector", {}))]


def _categories(rules: Rulebook, today) -> list[dict]:
    crit = rules.one("critical_classification", today)
    return crit.params.get("categories", []) if crit else []


def _key(comp: Component, rules: Rulebook, today, model: str) -> str:
    basis = {"model": model, "part": [comp.name, comp.description, comp.category, comp.unit_of_measure],
             "labels": [c["label"] for c in _categories(rules, today)],
             "rules": [(r.id, r.scope.get("description", "")) for r in worded_rules(rules, today)]}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:24]


def _ask(client: ModelProvider, parts: list[Component], rules: Rulebook, today) -> dict:
    cats = _categories(rules, today)
    crit = rules.one("critical_classification", today)
    policy = {
        "critical_categories": [c["label"] for c in cats],
        "critical_source": f"{crit.citation}: {crit.source.get('quote', '')}" if crit else "",
        "rules": [{"id": r.id, "applies_to": r.scope.get("description", ""), "source": r.citation,
                   "quote": r.source.get("quote", "")[:300]} for r in worded_rules(rules, today)],
    }
    items = [{"component_id": c.component_id, "name": c.name, "description": c.description,
              "catalog_category": c.category} for c in parts]
    raw = client.chat([{"role": "system", "content": PROMPT.read_text()},
                       {"role": "user", "content": f"POLICY\n{json.dumps(policy, indent=1)}\n\nPARTS\n{json.dumps(items, indent=1)}"}])
    m = re.search(r"\{.*\}", raw, re.S)
    reply = json.loads(m.group(0)) if m else None
    if not isinstance(reply, dict) or not isinstance(reply.get("parts"), list):
        raise ValueError("no 'parts' list in the answer")
    return reply


def label_parts(client: ModelProvider | None, state: ScenarioState, rules: Rulebook,
                cache_dir: Path | None = None) -> PartLabels:
    out = PartLabels()
    today = state.current_date
    if client is None:
        return out
    labels = {c["label"] for c in _categories(rules, today)}
    allowed = {r.id for r in worded_rules(rules, today)}
    cache_file = cache_dir / "part_labels.json" if cache_dir else None
    cache = {}
    if cache_file and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except ValueError:
            cache = {}

    keys = {cid: _key(c, rules, today, client.model_name) for cid, c in state.components.items()}
    answers = {cid: cache[k] for cid, k in keys.items() if k in cache}
    todo = [c for cid, c in sorted(state.components.items()) if cid not in answers]
    for i in range(0, len(todo), 25):                       # keep each request small enough for any model
        batch = todo[i:i + 25]
        try:
            reply = _ask(client, batch, rules, today)
        except (LLMError, ValueError) as e:
            out.alerts.append(Alert("INFO", f"Model part classification skipped for {len(batch)} part(s) "
                                            f"({str(e)[:100]}); the rulebook's keyword rules were used for them."))
            continue
        ids = {c.component_id for c in batch}
        for p in reply["parts"]:
            if not isinstance(p, dict) or p.get("component_id") not in ids:
                continue                                    # not a part we asked about
            cat = p.get("critical_category")
            answers[p["component_id"]] = {
                "critical": cat if cat in labels else None,                       # only the policy's labels
                "rules": sorted(set(p.get("rules") or []) & allowed),            # only known worded rules
                "reason": " ".join(str(p.get("reason", "")).split())[:200]}
    if cache_file and answers:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache.update({keys[cid]: a for cid, a in answers.items()})
            cache_file.write_text(json.dumps(cache, indent=1, sort_keys=True))
        except OSError:
            pass                                            # a read-only install still works, just uncached

    added, floor_kept = [], []
    by_id = {r.id: r for r in rules.rules}
    for cid, a in sorted(answers.items()):
        comp = state.components[cid]
        if a["critical"]:
            out.critical[cid] = a["critical"]
        if a["rules"]:
            out.scope[cid] = set(a["rules"])
        kw_critical = any(matches(c["selector"], comp) for c in _categories(rules, today))
        kw_rules = {r.id for r in worded_rules(rules, today) if r.applies_to(comp)}
        new = []
        if a["critical"] and not kw_critical:
            new.append(f"critical part ({a['critical']})")
        new += [f"{by_id[r].citation} ({by_id[r].scope.get('description', r)})" for r in sorted(set(a["rules"]) - kw_rules)]
        if new:
            out.holds[cid] = [f"classified by the model, not by the rulebook's keywords: {'; '.join(new)}"]
            added.append(f"{comp.name} ({cid}): {'; '.join(new)}" + (f" (model's reason: {a['reason']})" if a["reason"] else ""))
        missed = [f"{by_id[r].citation}" for r in sorted(kw_rules - set(a["rules"]))]
        if kw_critical and not a["critical"]:
            missed.insert(0, "critical part")
        if missed:
            floor_kept.append(f"{comp.name} ({cid}): {', '.join(missed)}")
    if added:
        out.alerts.append(Alert("WARNING", "The model applied policy rules the rulebook's keywords missed. Orders for these "
                                           "parts are held until a person confirms; if correct, add them to the rulebook: "
                                           + " | ".join(added)))
    if floor_kept:
        out.alerts.append(Alert("INFO", "The rulebook's keywords apply rules the model did not; the stricter reading was "
                                        "kept. Please confirm: " + " | ".join(floor_kept)))
    if answers:
        out.alerts.append(Alert("INFO", f"Parts classified by {client.model_name}: {len(answers)} of "
                                        f"{len(state.components)}, checked against the policy's own categories and rules."))
    return out
