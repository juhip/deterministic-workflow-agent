"""Layer 5b: the written rationale stored on each purchase order.

Built only from facts the code already computed (no free text from a model), so every
number in a rationale can be traced back to the data. A reader should be able to answer
"why this part, this quantity, this supplier, this date" from the rationale alone.
"""
from __future__ import annotations

from .classifier import ComponentTags
from .decider import ComponentDecision, PlannedPO
from .models import ScenarioState
from .rules import Rulebook


def _share(dec: ComponentDecision, pos: list[PlannedPO], state: ScenarioState, supplier_id: str) -> float:
    totals: dict[str, float] = {}
    for p in state.existing_pos:
        if p.component_id == dec.component_id:
            totals[p.supplier_id] = totals.get(p.supplier_id, 0) + p.quantity
    for p in pos:
        totals[p.supplier_id] = totals.get(p.supplier_id, 0) + p.quantity
    return totals.get(supplier_id, 0) / (sum(totals.values()) or 1)


def rationale(po: PlannedPO, dec: ComponentDecision, component_pos: list[PlannedPO], state: ScenarioState,
              rules: Rulebook, tags: ComponentTags, flags: list[str]) -> str:
    comp = state.components[po.component_id]
    sup = state.suppliers[po.supplier_id]
    req = dec.requirement
    k = (po.supplier_id, po.mode)
    opt = next(o for o in dec.options if o.supplier.supplier_id == po.supplier_id and o.mode == po.mode)
    unit = comp.unit_of_measure if comp.unit_of_measure not in ("", "each") else "units"
    uom = unit if unit in ("kg", "units") or unit.endswith("s") else unit + "s"   # "27 tubes", "20 cans"
    out: list[str] = []

    ship = " by air freight" if po.mode == "air" else ""
    out.append(f"Buy {po.quantity} {uom} of {comp.name} from {sup.name} ({sup.supplier_id}) at ${po.unit_price:,.2f} "
               f"= ${po.total:,.2f}; ordered {po.order_date}, {opt.lead_days}-day lead time{ship}, expected {po.expected_delivery_date}.")

    inbound = sum(q for _, q in req.inbound)
    orders = ", ".join(sorted({l.order.order_id for l in req.lines}))
    out.append(f"Need: {req.total_demand:g} {uom} for {orders}, minus {req.on_hand:g} on hand and {inbound:g} on open "
               f"orders, leaves {req.to_buy} to buy.")

    covers = []
    by_order: dict[str, int] = {}
    for oid, q in po.serves:
        by_order[oid] = by_order.get(oid, 0) + q
    for oid, q in by_order.items():
        order = next(o for o in state.schedule if o.order_id == oid)
        gap = (order.materials_needed_by - po.expected_delivery_date).days
        timing = f"{gap} days early" if gap > 0 else ("on the day" if gap == 0 else f"{-gap} days LATE")
        covers.append(f"{oid} ({order.customer}) {q} {uom}, arriving {timing} for its {order.materials_needed_by} need-by date")
    if covers:
        out.append("Covers: " + "; ".join(covers) + ".")
    surplus = dec.surplus.get(k, 0)
    if surplus:
        bought = sum(p.quantity for p in component_pos if (p.supplier_id, p.mode) == k)
        if bought == opt.moq:
            out.append(f"Includes {surplus} {uom} beyond the need because this supplier's minimum order is {opt.moq}.")
        else:
            out.append(f"Includes {surplus} {uom} beyond the need so the supplier split rule "
                       f"({dec.concentration.citation if dec.concentration else 'policy'}) can be met.")

    out.append(f"Why this supplier: {dec.choice_reasons.get(k, 'best remaining option under the policy')}.")
    if not opt.domestic:
        out.append(f"International sourcing justification: {dec.intl_reasons.get(k, 'see sourcing rules')}.")
    else:
        cheaper = [o for o in dec.eligible if not o.domestic and o.unit_price < opt.unit_price]
        if cheaper:
            c = min(cheaper, key=lambda o: o.unit_price)
            dp = rules.one("domestic_preference", po.order_date)
            if dp:
                thr = dp.params["premium_threshold_critical" if tags.critical else "premium_threshold"]
                prem = (opt.unit_price - c.unit_price) / c.unit_price
                if prem <= thr + 1e-9:
                    out.append(f"Domestic preferred over the cheaper {c.supplier.name} (${c.unit_price:,.2f}): the "
                               f"{prem:.0%} domestic premium does not exceed the {thr:.0%} threshold ({dp.citation}).")
    if tags.critical:
        out.append(f"Critical component: {tags.critical_reason}.")
    if dec.concentration:
        share = _share(dec, component_pos, state, po.supplier_id)
        prm = dec.concentration.params
        cap = prm.get("max_share") or prm.get("max_share_critical" if tags.critical else "max_share_noncritical", 1)
        basis = "every order" if prm.get("applies_per_order") else f"rolling {prm.get('window_months', 12)} months"
        out.append(f"This supplier's share of open and new orders for this part: {share:.0%} "
                   f"(limit {cap:.0%} over {basis}, {dec.concentration.citation}).")
    excluded = [o for o in dec.options if not o.eligible and o.mode == "standard"]
    if excluded:
        out.append("Not allowed: " + "; ".join(f"{o.supplier.name} ({o.supplier.supplier_id}) - {', '.join(o.excluded_because)}"
                                              for o in excluded) + ".")
    if tags.alias_notes:
        out.append("Note: " + "; ".join(tags.alias_notes) + ".")
    if flags:
        out.append("Flags: " + " | ".join(flags) + ".")
    return " ".join(out)
