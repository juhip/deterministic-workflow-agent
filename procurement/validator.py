"""Layer 5a: independent double-check of a finished plan.

Two checks, both written separately from the code that made the plan, so a mistake in
one is unlikely to be repeated in the other:

  replay()          walks through the calendar: stock + every delivery (existing and new)
                    against every production order, earliest deadline first. Reports when
                    each order's materials are complete and which part is holding it up.
  hard_violations() re-checks every purchase order against the hard rules (approved list,
                    catalog price, MOQ, delivery-date arithmetic, certifications, air-freight
                    window). Any violation blocks that order from being written.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from .classifier import ComponentTags
from .decider import PlannedPO
from .models import ProductionOrder, ScenarioState
from .options import exclusion_reasons, is_domestic
from .rules import Rulebook

EPS = 1e-6


@dataclass
class OrderStatus:
    order: ProductionOrder
    ready: date | None                 # when the last material is in hand; None = never fully supplied
    late_days: int | None
    limiting: list[tuple[str, date | None]] = field(default_factory=list)  # parts that set the ready date
    buildable_on_time: int = 0         # units buildable with material in hand by the need-by date

    @property
    def on_time(self) -> bool:
        return self.ready is not None and self.ready <= self.order.materials_needed_by


def replay(state: ScenarioState, pos: list[PlannedPO], buffer_days: dict[str, int]) -> list[OrderStatus]:
    events: dict[str, list[tuple[date, float]]] = {}
    for comp_id, qty in state.inventory.items():
        events.setdefault(comp_id, []).append((state.current_date, qty))
    for p in state.existing_pos:
        arrive = (p.expected_delivery_date or state.current_date) + timedelta(days=buffer_days.get(p.component_id, 0))
        events.setdefault(p.component_id, []).append((arrive, p.quantity))
    for p in pos:
        arrive = p.expected_delivery_date + timedelta(days=buffer_days.get(p.component_id, 0))
        events.setdefault(p.component_id, []).append((arrive, float(p.quantity)))
    for ev in events.values():
        ev.sort()

    used: dict[str, float] = {}  # cumulative demand already claimed per component
    out: list[OrderStatus] = []
    for order in state.schedule:  # earliest deadline first
        bom = state.bom.get(order.product_id, {})
        if not bom or order.quantity <= 0:
            continue
        readies: list[tuple[str, date | None]] = []
        buildable = order.quantity
        when = max(order.materials_needed_by, state.current_date)
        for comp_id, per in bom.items():
            need = per * order.quantity
            before = used.get(comp_id, 0.0)
            used[comp_id] = before + need
            running, ready = 0.0, None
            for d, q in events.get(comp_id, []):
                running += q
                if running >= used[comp_id] - EPS:
                    ready = d
                    break
            readies.append((comp_id, ready))
            have = sum(q for d, q in events.get(comp_id, []) if d <= when) - before
            buildable = min(buildable, int(math.floor(max(0.0, min(have, need)) / per + EPS)))
        if any(r is None for _, r in readies):
            out.append(OrderStatus(order, None, None, [(c, r) for c, r in readies if r is None], buildable))
            continue
        ready = max(max(r for _, r in readies), state.current_date)
        limiting = [(c, r) for c, r in readies if r == ready and r > order.materials_needed_by]
        out.append(OrderStatus(order, ready, (ready - order.materials_needed_by).days, limiting, buildable))
    return out


def hard_violations(state: ScenarioState, rules: Rulebook, tags: dict[str, ComponentTags],
                    pos: list[PlannedPO]) -> list[tuple[PlannedPO, str]]:
    catalog = {(e.supplier_id, e.component_id): e for e in state.catalog}
    air_rules = rules.active("expedited_shipping", state.current_date)
    bad: list[tuple[PlannedPO, str]] = []
    for p in pos:
        sup = state.suppliers.get(p.supplier_id)
        entry = catalog.get((p.supplier_id, p.component_id))
        if sup is None or entry is None:
            bad.append((p, "supplier does not offer this part in the catalog"))
            continue
        if not sup.on_approved_list:
            bad.append((p, "supplier is not on the Approved Supplier List"))
        if abs(p.unit_price - entry.unit_price) > 1e-9:
            bad.append((p, f"unit price {p.unit_price} differs from catalog {entry.unit_price}"))
        if p.quantity != int(p.quantity) or p.quantity < entry.minimum_order_qty:
            bad.append((p, f"quantity {p.quantity} is below the MOQ of {entry.minimum_order_qty}"))
        lead = (p.expected_delivery_date - p.order_date).days
        if p.mode == "standard" and lead != entry.lead_time_days:
            bad.append((p, f"delivery date implies {lead} days; catalog lead time is {entry.lead_time_days}"))
        if p.mode == "air":
            if not air_rules:
                bad.append((p, "air freight used outside its authorization window"))
            elif is_domestic(sup, rules, state.current_date):
                bad.append((p, "air freight is only authorized for international suppliers"))
            else:
                prm = air_rules[0].params
                expect = max(prm["min_lead_time_days"], entry.lead_time_days - prm["lead_time_reduction_days"])
                if lead != expect:
                    bad.append((p, f"air-freight delivery implies {lead} days; expected {expect}"))
        for reason in exclusion_reasons(sup, tags[p.component_id], state, rules, p.component_id):
            bad.append((p, reason))
    return bad
