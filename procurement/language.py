"""AI checks inside the agent's run: the two places where the input is language.

  (part classification lives in part_labels.py: the model reads every part before planning)
  read_supplier_notes The model reads the free-text notes the company keeps about each supplier in
                      today's plan (e.g. "small batch capacity") and reports risks. A risk only
                      counts if it quotes the note word for word. Matching orders are held.

The model can only make the agent MORE cautious: it can hold an order and raise an alert. It
never chooses a supplier, changes a quantity or releases an order. Every answer is checked in
code, and if the model is missing, slow or wrong, the run carries on without it and says so.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .alerts import Alert
from .classifier import ComponentTags
from .decider import PlannedPO
from .llm import LLMError, ModelProvider
from .models import ScenarioState
from .rules import Rulebook

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class AIChecks:
    """What the model found, ready for the release step and the alerts."""
    holds_by_component: dict[str, list[str]] = field(default_factory=dict)   # part -> reasons to hold
    holds_by_order: dict[int, list[str]] = field(default_factory=dict)       # index in pos -> reasons
    alerts: list[Alert] = field(default_factory=list)


def _json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except ValueError:
        return None


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


# ---- 2. supplier notes ------------------------------------------------------------------------
def read_supplier_notes(client: ModelProvider, state: ScenarioState, pos: list[PlannedPO], checks: AIChecks) -> None:
    catalog = {(e.supplier_id, e.component_id): e for e in state.catalog}
    orders, notes_by_label = [], {}
    for i, p in enumerate(pos):
        sup = state.suppliers[p.supplier_id]
        notes = [n for n in (sup.notes, catalog[(p.supplier_id, p.component_id)].notes) if n and n.strip()]
        if not notes:
            continue
        label = f"O{i + 1}"
        notes_by_label[label] = (i, notes)
        orders.append({"label": label, "supplier": sup.name, "part": state.components[p.component_id].name,
                       "quantity": p.quantity, "supplier_notes": sup.notes,
                       "catalog_notes": catalog[(p.supplier_id, p.component_id)].notes})
    if not orders:
        return
    messages = [{"role": "system", "content": (PROMPTS / "supplier_notes.md").read_text()},
                {"role": "user", "content": f"ORDERS\n{json.dumps(orders, indent=1)}"}]
    try:
        reply = _json(client.chat(messages))
    except LLMError as e:
        checks.alerts.append(Alert("INFO", f"AI supplier-note check skipped ({str(e)[:100]})."))
        return
    if not reply or not isinstance(reply.get("risks"), list):
        checks.alerts.append(Alert("INFO", "AI supplier-note check returned an unusable answer and was ignored."))
        return
    found = 0
    for r in reply["risks"][:10]:
        if not isinstance(r, dict) or r.get("order") not in notes_by_label:
            continue                                                  # refers to an order not in the plan
        i, notes = notes_by_label[r["order"]]
        quote = str(r.get("quote", "")).strip()
        if not quote or not any(_norm(quote) in _norm(n) for n in notes):
            continue                                                  # quote not in the notes: invented, drop it
        p = pos[i]
        risk = " ".join(str(r.get("risk", "")).split())[:300]
        found += 1
        checks.holds_by_order.setdefault(i, []).append(f'AI check: supplier note "{quote}" signals a risk')
        checks.alerts.append(Alert("WARNING", f"Supplier note risk for {state.components[p.component_id].name} from "
                                              f"{state.suppliers[p.supplier_id].name} ({p.quantity} units): the note says "
                                              f'"{quote}". {risk} The order is held; confirm with the supplier before release.'))
    checks.alerts.append(Alert("INFO", f"AI supplier-note check ({client.model_name}) read the notes for {len(orders)} "
                                       f"order(s); {found} risk(s) found."))


def run_ai_checks(client: ModelProvider, state: ScenarioState, rules: Rulebook, tags: dict[str, ComponentTags],
                  pos: list[PlannedPO]) -> AIChecks:
    checks = AIChecks()
    parts = sorted({p.component_id for p in pos})
    if parts:
        read_supplier_notes(client, state, pos, checks)
    return checks
