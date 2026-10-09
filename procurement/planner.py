"""Layer 2: the shopping list ("netting").

For every component, work out how many units must be bought and by which date.

Method (earliest deadline first):
  * Each production order consumes  quantity x quantity_per  of each component in its BOM.
  * Orders are served in order of materials_needed_by (ties broken by order_id), so stock
    goes to whichever customer needs it first.
  * Stock on hand and everything already on order count in full. The agent never buys
    duplicate stock: if an existing order arrives after a deadline, the double-check step
    reports that production order as late and suggests expediting, instead of buying twice.
  * Walking through the orders, the first time cumulative demand exceeds stock + all open
    orders, the uncovered units become a "tranche": a quantity, the date it is needed by,
    and the production orders it is for.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .models import ExistingPO, ProductionOrder, ScenarioState

EPS = 1e-6  # guards against float noise like 0.1 + 0.2 != 0.3 before rounding up


@dataclass
class DemandLine:
    order: ProductionOrder
    qty: float  # units of this component the order consumes (may be fractional, e.g. 6.25 cans)


@dataclass
class Tranche:
    need_by: date
    qty: int                                              # whole units to buy
    orders: list[tuple[str, int]] = field(default_factory=list)  # (production order id, units)


@dataclass
class Requirement:
    component_id: str
    lines: list[DemandLine]
    on_hand: float
    inbound: list[tuple[date, float]]  # existing POs: (expected delivery, qty)
    tranches: list[Tranche]

    @property
    def total_demand(self) -> float:
        return sum(l.qty for l in self.lines)

    @property
    def to_buy(self) -> int:
        return sum(t.qty for t in self.tranches)


def build_demand(state: ScenarioState) -> dict[str, list[DemandLine]]:
    """component_id -> demand lines, earliest need-by first."""
    demand: dict[str, list[DemandLine]] = {}
    for order in state.schedule:  # already sorted by (need-by, order_id)
        if order.quantity <= 0:
            continue
        for comp_id, per_unit in state.bom.get(order.product_id, {}).items():
            demand.setdefault(comp_id, []).append(DemandLine(order, per_unit * order.quantity))
    return demand


def inbound_for(existing: list[ExistingPO], component_id: str, today: date) -> list[tuple[date, float]]:
    # A PO with no delivery date is assumed to arrive today (and flagged later as a data issue).
    return sorted((p.expected_delivery_date or today, p.quantity) for p in existing if p.component_id == component_id)



def net_component(component_id: str, lines: list[DemandLine], on_hand: float,
                  inbound: list[tuple[date, float]], today: date) -> Requirement:
    tranches: list[Tranche] = []
    available = on_hand + sum(q for _, q in inbound)   # everything we have or have already ordered
    cumulative = 0.0
    bought_so_far = 0
    for line in lines:
        cumulative += line.qty
        uncovered = max(0, math.ceil(cumulative - available - EPS))
        extra = uncovered - bought_so_far
        if extra <= 0:
            continue
        bought_so_far = uncovered
        if tranches and tranches[-1].need_by == line.order.materials_needed_by:
            tranches[-1].qty += extra
            tranches[-1].orders.append((line.order.order_id, extra))
        else:
            tranches.append(Tranche(line.order.materials_needed_by, extra, [(line.order.order_id, extra)]))
    return Requirement(component_id, lines, on_hand, inbound, tranches)


def net_requirements(state: ScenarioState) -> dict[str, Requirement]:
    """Shopping list for the whole scenario: component_id -> Requirement."""
    out = {}
    for comp_id, lines in build_demand(state).items():
        out[comp_id] = net_component(comp_id, lines, state.inventory.get(comp_id, 0.0),
                                     inbound_for(state.existing_pos, comp_id, state.current_date),
                                     state.current_date)
    return out
