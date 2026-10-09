"""One full run, in four stages: interpret (parts and rules in force) -> decide (net, choose suppliers)
-> verify (hard-rule gate, replay, supplier-note check) -> escalate (release or hold, recovery options).

Writing to the database (the "act" step) is done by writer.py, called from agent.py, so
the whole plan can be inspected (or run as a dry run) before anything is written.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .alerts import ORDER, Alert, build_alerts, po_flags
from .classifier import ComponentTags, classify_all
from .decider import ComponentDecision, Decider, PlannedPO
from .explain import rationale
from .models import ScenarioState
from .planner import net_requirements
from .language import run_ai_checks
from .messages import rewrite_alerts
from .part_labels import PartLabels, label_parts
from .exceptions import as_alert, recommend, recovery_options
from .llm import ModelProvider
from .release import Release, decide_release
from .rules import Rulebook
from .validator import OrderStatus, hard_violations, replay


@dataclass
class RunResult:
    pos: list[PlannedPO]
    alerts: list[Alert]
    statuses: list[OrderStatus]
    decisions: dict[str, ComponentDecision]
    tags: dict[str, ComponentTags] = field(default_factory=dict)
    releases: list = field(default_factory=list)   # one Release per purchase order, same order
    part_labels: PartLabels | None = None          # the model's verified part labels (reused by re-runs)


def run(state: ScenarioState, rules: Rulebook, model: ModelProvider | None = None, explore: bool = True,
        part_labels: PartLabels | None = None, cache_dir: Path | None = None) -> RunResult:
    """One run. `model` is optional: without it the AI steps are simply skipped. `explore` works out
    recovery options for late orders (off for the re-runs that test those options)."""
    rules, policy_note = rules.base_policy_in_force(state.current_date)
    # understand the parts: the model reads every part first; the keyword rules are the floor
    if part_labels is None:
        use_model = model is not None and rules.raw.get("settings", {}).get("classify_parts_with_model", True)
        part_labels = label_parts(model if use_model else None, state, rules, cache_dir)
    rules = rules.with_extra_scope(part_labels.scope)
    tags = classify_all(state.components, rules, state.current_date, part_labels.critical)
    requirements = net_requirements(state)                                        # what is short, by when
    decider = Decider(state, rules, tags)
    decisions = {c: decider.decide(r) for c, r in sorted(requirements.items()) if r.tranches}
    pos: list[PlannedPO] = []
    for dec in decisions.values():
        pos.extend(decider.purchase_orders(dec))

    # check: anything breaking a hard rule is removed before it can be written
    violations = hard_violations(state, rules, tags, pos)
    blocked = {id(p) for p, _ in violations}
    pos = [p for p in pos if id(p) not in blocked]
    pos.sort(key=lambda p: (p.expected_delivery_date, p.component_id, p.supplier_id))

    buffers = {c: decider.buffer_days(c) for c in state.components}
    statuses = replay(state, pos, buffers)

    # optional AI checks: may only hold orders for review, never release or change them
    ai = run_ai_checks(model, state, rules, tags, pos) if model else None

    flags: dict[int, list[str]] = {}
    releases: list[Release] = []
    for i, po in enumerate(pos):
        dec = decisions[po.component_id]
        urgent = any(
            (o.materials_needed_by - po.expected_delivery_date).days <= 7
            for o in state.schedule if o.order_id in {oid for oid, _ in po.serves})
        flags[i] = po_flags(po, dec, state, rules, tags[po.component_id], urgent)
        same = [p for p in pos if p.component_id == po.component_id]
        po.rationale = rationale(po, dec, same, state, rules, tags[po.component_id], flags[i])
        ai_reasons = (ai.holds_by_component.get(po.component_id, []) + ai.holds_by_order.get(i, [])) if ai else []
        ai_reasons = part_labels.holds.get(po.component_id, []) + ai_reasons
        rel = decide_release(po, dec, state, rules, tags[po.component_id], ai_reasons)
        releases.append(rel)
        po.rationale = ((f"DRAFT, awaiting approval by {rel.approver} before release to the supplier "
                         f"(because: {'; '.join(rel.reasons)}). ") if rel.needs_approval else
                        "RELEASED automatically: routine order within the approval guardrails. ") + po.rationale

    alerts = build_alerts(state, rules, tags, decisions, pos, flags, statuses, violations, releases)
    if policy_note:
        alerts.insert(0, Alert("WARNING", policy_note))
    if part_labels.alerts:
        summary = alerts.pop()
        alerts = sorted(alerts + part_labels.alerts, key=lambda a: ORDER[a.severity]) + [summary]
    if ai:
        summary = alerts.pop()                                    # keep the run summary last
        alerts = sorted(alerts + ai.alerts, key=lambda a: ORDER[a.severity]) + [summary]

    # exceptions: for every late order, checked recovery options (and an optional model recommendation)
    if explore and rules.raw.get("settings", {}).get("recovery_options"):
        result = RunResult(pos, alerts, statuses, decisions, tags, releases, part_labels)
        try:                                          # advice must never stop the plan from being written
            exceptions = recovery_options(state, rules, result)
        except Exception as e:                        # noqa: BLE001
            exceptions = []
            alerts.insert(len(alerts) - 1, Alert("WARNING", f"Recovery options could not be worked out ({type(e).__name__}: "
                                                            f"{str(e)[:120]}). The plan itself is unaffected."))
        picks, note = (recommend(model, state, rules, exceptions) if model and exceptions else ({}, None))
        summary = alerts.pop()
        alerts = alerts + [as_alert(ex, rules, picks.get(ex.order_id, "")) for ex in exceptions]
        alerts = sorted(alerts, key=lambda a: ORDER[a.severity]) + ([note] if note else []) + [summary]

    # optional: the model rewrites the messages people act on; code checks every fact first
    if model and rules.raw.get("settings", {}).get("rewrite_messages_with_model"):
        summary = alerts.pop()
        alerts, note = rewrite_alerts(model, state, rules, alerts)
        alerts = alerts + [note, summary]
    return RunResult(pos, alerts, statuses, decisions, tags, releases, part_labels)
