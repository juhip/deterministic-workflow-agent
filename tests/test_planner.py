"""Netting tests. Expected numbers were worked out by hand from the scenario data."""
import math
from datetime import date

import pytest

from conftest import ALL_SCENARIOS
from procurement.db import load_state
from procurement.models import ProductionOrder
from procurement.planner import DemandLine, net_component, net_requirements

D = date.fromisoformat


def tranches(req):
    return [(t.need_by.isoformat(), t.qty) for t in req.tranches]


# ---- scenario 01 ----------------------------------------------------------------------
def test_s01_magnets(scenario_copy):
    # WO-7101 needs 24x6=144 by 10-16; WO-7104 needs 9x12=108 by 11-13; 90 on hand.
    r = net_requirements(load_state(scenario_copy("scenario_01_baseline.sqlite")))["PT-103"]
    assert tranches(r) == [("2026-10-16", 54), ("2026-11-13", 108)]
    assert r.tranches[0].orders == [("WO-7101", 54)]


def test_s01_pcbs_shared_across_four_orders(scenario_copy):
    # Cumulative demand 24, 42, 66, 84 against 36 on hand -> short 0, 6, 30, 48
    r = net_requirements(load_state(scenario_copy("scenario_01_baseline.sqlite")))["PT-105"]
    assert tranches(r) == [("2026-10-18", 6), ("2026-10-25", 24), ("2026-11-13", 18)]


def test_s01_fractional_quantities_round_up(scenario_copy):
    # Conformal varnish: 6, 6, 4.5 cans against 4 on hand -> ceil(2)=2, ceil(8)=8, ceil(12.5)=13
    r = net_requirements(load_state(scenario_copy("scenario_01_baseline.sqlite")))["PT-111"]
    assert tranches(r) == [("2026-10-16", 2), ("2026-10-25", 6), ("2026-11-13", 5)]
    assert r.to_buy == 13


def test_s01_stock_covers_early_order_only(scenario_copy):
    # Shaft seals: 50 on hand covers WO-7101's 48; WO-7104's 18 leave 16 short by 11-13.
    reqs = net_requirements(load_state(scenario_copy("scenario_01_baseline.sqlite")))
    assert tranches(reqs["PT-109"]) == [("2026-11-13", 16)]
    assert reqs["PT-119"].tranches == []  # harness cable fully in stock


# ---- scenario 02: existing POs ---------------------------------------------------------
def test_s02_existing_pos_reduce_need(scenario_copy):
    reqs = net_requirements(load_state(scenario_copy("scenario_02_partial_procurement.sqlite")))
    # magnets: 90 stock + 20 (arrives 10-07) + 120 (10-08) = 230 by 10-16 >= 144; 252-230 = 22 by 11-13
    assert tranches(reqs["PT-103"]) == [("2026-11-13", 22)]
    assert reqs["PT-102"].tranches == []   # lamination PO of 160 plus stock covers everything
    assert tranches(reqs["PT-105"]) == [("2026-11-13", 8)]   # 36 stock + 40 on order vs 84


# ---- scenario 03: rush order jumps the queue -------------------------------------------
def test_s03_rush_order_takes_stock_first(scenario_copy):
    r = net_requirements(load_state(scenario_copy("scenario_03_tight_timeline.sqlite")))["PT-105"]
    assert tranches(r) == [("2026-10-13", 54), ("2026-10-16", 24), ("2026-10-18", 18),
                           ("2026-10-25", 24), ("2026-11-13", 18)]
    assert r.tranches[0].orders == [("WO-7105", 54)]


# ---- scenario 06: the simple case ------------------------------------------------------
def test_s06_only_two_shortfalls(scenario_copy):
    reqs = net_requirements(load_state(scenario_copy("scenario_06_simple.sqlite")))
    short = {c: r.to_buy for c, r in reqs.items() if r.tranches}
    assert short == {"PT-114": 10, "PT-116": 16}


# ---- edge cases with synthetic data ----------------------------------------------------
def _order(oid, need_by, qty=1):
    return ProductionOrder(oid, "ASM-X", qty, "Cust", D(need_by))


def test_existing_orders_count_in_full_even_if_late():
    # 0 stock; an existing PO of 50 lands 10-24, after A's 10-14 deadline. The agent does not
    # double-buy for A (the double-check flags A as late instead); it buys only the 10 never covered.
    lines = [DemandLine(_order("A", "2026-10-14"), 30), DemandLine(_order("B", "2026-11-05"), 30)]
    r = net_component("C", lines, 0, [(D("2026-10-24"), 50)], D("2026-10-05"))
    assert tranches(r) == [("2026-11-05", 10)]


def test_past_due_order_is_judged_against_today():
    lines = [DemandLine(_order("A", "2026-09-19"), 10)]
    r = net_component("C", lines, 4, [], D("2026-10-05"))
    assert tranches(r) == [("2026-09-19", 6)]  # keeps the real need-by so lateness can be reported


def test_same_day_orders_merge_into_one_tranche():
    lines = [DemandLine(_order("A", "2026-10-14"), 5), DemandLine(_order("B", "2026-10-14"), 7)]
    r = net_component("C", lines, 0, [], D("2026-10-05"))
    assert tranches(r) == [("2026-10-14", 12)]
    assert r.tranches[0].orders == [("A", 5), ("B", 7)]


# ---- properties that must hold for every scenario --------------------------------------
@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_never_buys_less_than_the_overall_gap(scenario_copy, name):
    state = load_state(scenario_copy(name))
    for comp_id, r in net_requirements(state).items():
        all_supply = r.on_hand + sum(q for _, q in r.inbound)
        assert r.to_buy >= max(0, math.ceil(r.total_demand - all_supply - 1e-6)), comp_id
        assert all(t.qty > 0 for t in r.tranches)
        assert [t.need_by for t in r.tranches] == sorted(t.need_by for t in r.tranches)
        assert sum(q for t in r.tranches for _, q in t.orders) == r.to_buy
