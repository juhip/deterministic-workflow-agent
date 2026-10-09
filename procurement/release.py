"""Who has to approve an order before it is released to the supplier.

Three modes, set by `settings.order_release` in the rulebook:

  approve_by_exception (default)  Routine orders are released automatically. An order
                                  waits for a person if any enabled exception applies
                                  (critical part, memo applies, international supplier,
                                  over the value cap, ...). Exceptions are listed in
                                  `settings.approval_exceptions` so the business can tune them.
  approve_every_order             Every order waits for a person (most cautious).
  policy_thresholds_only          Only what the policy itself names needs approval
                                  (air freight, over the policy's approval levels).

The policy's own approval thresholds always apply on top, and decide WHO approves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .classifier import ComponentTags
from .decider import ComponentDecision, PlannedPO
from .models import ScenarioState
from .rules import Rulebook

DEFAULT_EXCEPTIONS = {
    "max_order_value": None,       # the limit itself lives in settings.approval_exceptions
    "critical_part": True,
    "memo_applies": True,
    "international_supplier": True,
    "below_B_supplier": True,
    "air_freight": True,
    "hazardous": True,
    "single_allowed_supplier": True,
    "arrives_late": True,
    "rule_conflict": True,
    "ai_flag": True,
}


@dataclass
class Release:
    needs_approval: bool
    approver: str = ""
    reasons: list[str] = field(default_factory=list)
    slack_days: int = 0          # days between arrival and the earliest need-by it serves (negative = late)


def mode(rules: Rulebook) -> str:
    return rules.raw.get("settings", {}).get("order_release", "approve_by_exception")


def approval_levels(rules: Rulebook, today: date) -> list[tuple[float, str]]:
    """The policy's value thresholds, lowest first, as (threshold, approver title) pairs."""
    ap = rules.one("approval_thresholds", today)
    return sorted((lvl["above"], lvl["approver"]) for lvl in ap.params.get("approval_levels", [])) if ap else []


def default_approver(rules: Rulebook) -> str:
    return rules.raw.get("settings", {})["default_approver"]


def approvers_by_authority(rules: Rulebook, today: date) -> list[str]:
    """Lowest to highest authority, all read from the rulebook."""
    out = [default_approver(rules)]
    for _, who in approval_levels(rules, today):
        if who not in out:
            out.append(who)
    return out


def approver_for(total: float, rules: Rulebook, today: date) -> str:
    who = default_approver(rules)
    for above, approver in approval_levels(rules, today):
        if total > above:
            who = approver
    return who


def conflict_decider(rules: Rulebook) -> str:
    """Who decides when following a rule makes a customer order late (a setting, not policy)."""
    return rules.raw.get("settings", {})["conflict_decided_by"]


def air_freight_approver(rules: Rulebook, today: date) -> str | None:
    air = rules.one("expedited_shipping", today)
    return air.params.get("approver") if air else None


def _at_least(current: str, minimum: str | None, rules: Rulebook, today: date) -> str:
    if not minimum:
        return current
    order = approvers_by_authority(rules, today)
    rank = lambda who: order.index(who) if who in order else 1
    return minimum if rank(minimum) > rank(current) else current


def strategic_shift(po: PlannedPO, dec: ComponentDecision, rules: Rulebook, today: date,
                    need_by: list[date], state: ScenarioState) -> str:
    """Policy section 9: moving volume away from a Strategic supplier needs VP approval.
    Counts only when a Strategic supplier that sells the part could have delivered on time
    (a choice, not forced by lead time) and the order is above the 'significant' setting."""
    strat = rules.one("strategic_preference", today)
    if not strat or not strat.params.get("shift_away_approver"):
        return ""
    tier = strat.params["tier"]
    if state.suppliers[po.supplier_id].relationship_tier == tier:
        return ""
    if po.total <= rules.raw.get("settings", {}).get("strategic_shift_significant_above", 0):
        return ""
    deadline = min(need_by, default=None)
    passed = sorted({o.supplier.name for o in dec.eligible if o.supplier.relationship_tier == tier
                     and (deadline is None or o.arrival <= deadline)})
    if not passed:
        return ""
    return (f"moves volume away from {tier} supplier {', '.join(passed)}, who could deliver on time; "
            f"needs {strat.params['shift_away_approver']} approval ({strat.citation})")


def decide_release(po: PlannedPO, dec: ComponentDecision, state: ScenarioState, rules: Rulebook,
                   tags: ComponentTags, ai_reasons: list[str] | None = None) -> Release:
    """ai_reasons: holds raised by the optional AI checks (they can only add caution)."""
    today = state.current_date
    opt = next(o for o in dec.options if o.supplier.supplier_id == po.supplier_id and o.mode == po.mode)
    need_by = [o.materials_needed_by for o in state.schedule if o.order_id in {oid for oid, _ in po.serves}]
    slack = min(((d - po.expected_delivery_date).days for d in need_by), default=0)

    # what the policy itself requires, in every mode
    named: list[str] = []
    ap = rules.one("approval_thresholds", today)
    levels = approval_levels(rules, today)
    if ap and levels and po.total > levels[0][0]:
        named.append(f"order value ${po.total:,.0f} is over the policy approval threshold ({ap.citation})")
    if po.mode == "air":
        named.append(f"air freight needs {air_freight_approver(rules, today)} approval for each request")
    shift = strategic_shift(po, dec, rules, today, need_by, state)
    if shift:
        named.append(shift)

    m = mode(rules)
    if m == "policy_thresholds_only":
        reasons = named
    elif m == "approve_every_order":
        reasons = named or ["supervised mode: every order is approved by a person"]
    else:
        ex = {**DEFAULT_EXCEPTIONS, **rules.raw.get("settings", {}).get("approval_exceptions", {})}
        reasons = list(named)
        cap = ex.get("max_order_value")
        if cap and po.total > cap and not named:
            reasons.append(f"order value ${po.total:,.0f} is above the ${cap:,} auto-release limit")
        # critical_part / single_allowed_supplier take True, False or a narrower mode:
        #   critical_part "single_sourced_only": hold a critical part only when it has one allowed supplier
        #     (a label from the model alone is already held through ai_flag)
        #   single_allowed_supplier "critical_only": the policy's two-supplier rule (§4) covers critical parts only
        single = len({o.supplier.supplier_id for o in dec.eligible}) == 1
        crit_mode = ex.get("critical_part")
        if tags.critical and (crit_mode is True or (crit_mode == "single_sourced_only" and single)):
            reasons.append(f"critical part ({tags.critical_reason})")
        memos = sorted({r.source["document"] for r in rules.rules if r.id in tags.rule_ids
                        and r.source["document"].upper().startswith("MEMO")})
        if ex.get("memo_applies") and memos:
            reasons.append(f"a management memo applies to this part ({', '.join(memos)})")
        if ex.get("international_supplier") and not opt.domestic:
            reasons.append(f"international supplier ({opt.supplier.country})")
        sus = rules.one("sustainability_preference", today)
        if ex.get("below_B_supplier") and sus and rules.rating_rank(opt.supplier.sustainability_rating) < \
                rules.rating_rank(sus.params["last_resort_below_rating"]):
            reasons.append(f"supplier rated {opt.supplier.sustainability_rating}, below "
                           f"{sus.params['last_resort_below_rating']}")
        if ex.get("hazardous") and tags.hazardous:
            reasons.append("hazardous material (procurement review required)")
        single_mode = ex.get("single_allowed_supplier")
        if single and (single_mode is True or (single_mode == "critical_only" and tags.critical)):
            reasons.append("only one supplier is allowed for this part")
        if ex.get("arrives_late") and slack < 0:
            reasons.append(f"arrives {-slack} day(s) after a customer need-by date")
        if ex.get("rule_conflict") and (dec.conflicts or dec.alternative):
            reasons.append("part of a policy-versus-schedule conflict that needs a decision")

    ex_all = {**DEFAULT_EXCEPTIONS, **rules.raw.get("settings", {}).get("approval_exceptions", {})}
    if ai_reasons and ex_all.get("ai_flag"):
        reasons = reasons + [r for r in ai_reasons if r not in reasons]
    if not reasons:
        return Release(False, "", [], slack)
    # value sets the approver; air freight and rule-vs-schedule conflicts need at least their named decider
    who = approver_for(po.total, rules, today)
    if po.mode == "air":
        who = _at_least(who, air_freight_approver(rules, today), rules, today)
    if any(r.startswith("moves volume away from") for r in reasons):
        who = _at_least(who, rules.one("strategic_preference", today).params["shift_away_approver"], rules, today)
    if dec.conflicts or dec.alternative:
        who = _at_least(who, conflict_decider(rules), rules, today)
    return Release(True, who, reasons, slack)
