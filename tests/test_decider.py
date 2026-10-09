"""Supplier-choice tests: specific policy outcomes, then invariants on every scenario."""
import copy
from datetime import timedelta

import pytest

from conftest import ALL_SCENARIOS
from procurement.classifier import classify_all
from procurement.db import load_state
from procurement.decider import Decider
from procurement.planner import net_requirements
from procurement.rules import Rulebook

RULES = Rulebook.load()


def plan(scenario_copy, name, rules=RULES):
    state = load_state(scenario_copy(name))
    tags = classify_all(state.components, rules, state.current_date)
    d = Decider(state, rules, tags)
    decisions = {c: d.decide(r) for c, r in net_requirements(state).items() if r.tranches}
    pos = {c: d.purchase_orders(dec) for c, dec in decisions.items()}
    return state, decisions, pos


def by_supplier(pos_for_component):
    return {p.supplier_id: p.quantity for p in pos_for_component}


# ---- magnets: the memo split -------------------------------------------------------------
def test_s01_magnets_split_at_the_60_percent_cap(scenario_copy):
    _, dec, pos = plan(scenario_copy, "scenario_01_baseline.sqlite")
    assert by_supplier(pos["PT-103"]) == {"VEN-308": 65, "VEN-307": 97}      # 97 of 162 = 60%
    assert dec["PT-103"].conflicts == []


def test_s02_existing_orders_make_exact_split_impossible(scenario_copy):
    _, dec, pos = plan(scenario_copy, "scenario_02_partial_procurement.sqlite")
    assert by_supplier(pos["PT-103"]) == {"VEN-308": 45}
    assert dec["PT-103"].conflicts and "65%" in dec["PT-103"].conflicts[0]


def test_s04_buys_bounded_surplus_to_keep_split_without_extra_delay(scenario_copy):
    _, dec, pos = plan(scenario_copy, "scenario_04_low_inventory.sqlite")
    split = by_supplier(pos["PT-103"])
    assert max(split.values()) <= 0.6 * sum(split.values()) and min(split.values()) >= 0.25 * sum(split.values())
    need = dec["PT-103"].requirement.to_buy
    assert sum(split.values()) - need <= 0.25 * need + 3
    assert all(p.mode == "standard" for p in pos["PT-103"])


def test_s05_follows_rule_and_reports_schedule_alternative(scenario_copy):
    _, dec, pos = plan(scenario_copy, "scenario_05_competing_demand.sqlite")
    assert by_supplier(pos["PT-103"]) == {"VEN-308": 137, "VEN-307": 205}
    assert "100%" in dec["PT-103"].alternative and "5 to 0 days" in dec["PT-103"].alternative


def test_s05_protect_schedule_setting_flips_the_decision(scenario_copy):
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["when_rule_conflicts_with_schedule"] = "protect_schedule"
    _, dec, pos = plan(scenario_copy, "scenario_05_competing_demand.sqlite", Rulebook(raw))
    assert by_supplier(pos["PT-103"]) == {"VEN-308": 342}
    assert "Following the concentration rule" in dec["PT-103"].alternative


# ---- other policy choices ------------------------------------------------------------------
def test_domestic_wins_when_premium_is_exactly_the_threshold(scenario_copy):
    # IGBT in s05: VEN-301 $2.90 vs VEN-303 $2.00 (on time) -> premium exactly 45%; policy says "more than"
    _, _, pos = plan(scenario_copy, "scenario_05_competing_demand.sqlite")
    assert by_supplier(pos["PT-107"]) == {"VEN-301": 186}


def test_international_used_when_premium_exceeds_threshold(scenario_copy):
    # Flow sensor in s05: VEN-312 $8.10 vs VEN-303 $5.00 -> 62% > 45%, and VEN-303 is on time
    _, dec, pos = plan(scenario_copy, "scenario_05_competing_demand.sqlite")
    assert by_supplier(pos["PT-113"]) == {"VEN-303": 40}
    assert "62%" in dec["PT-113"].intl_reasons[("VEN-303", "standard")]


def test_strategic_partner_kept_when_saving_is_small(scenario_copy):
    # Potting resin: one supplier for both deadlines; Strategic VEN-301 rather than two POs
    _, _, pos = plan(scenario_copy, "scenario_01_baseline.sqlite")
    assert by_supplier(pos["PT-110"]) == {"VEN-301": 15}


def test_moq_surplus_recorded(scenario_copy):
    # Laminations: need 9; VEN-302 MOQ 20 is cheaper overall than VEN-305 MOQ 25
    _, dec, pos = plan(scenario_copy, "scenario_01_baseline.sqlite")
    assert by_supplier(pos["PT-102"]) == {"VEN-302": 20}
    assert dec["PT-102"].surplus == {("VEN-302", "standard"): 11}


def test_s06_exactly_two_orders(scenario_copy):
    _, _, pos = plan(scenario_copy, "scenario_06_simple.sqlite")
    flat = [p for ps in pos.values() for p in ps]
    assert {(p.component_id, p.supplier_id, p.quantity) for p in flat} == {("PT-114", "VEN-312", 10), ("PT-116", "VEN-302", 16)}


def test_no_air_freight_after_window(scenario_copy):
    _, _, pos = plan(scenario_copy, "scenario_05_competing_demand.sqlite")
    assert all(p.mode == "standard" for ps in pos.values() for p in ps)


# ---- invariants on every scenario ------------------------------------------------------
@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_invariants(scenario_copy, name):
    state, decisions, pos = plan(scenario_copy, name)
    catalog = {(e.supplier_id, e.component_id): e for e in state.catalog}
    for comp_id, dec in decisions.items():
        assert not dec.no_eligible_supplier
        bought = sum(p.quantity for p in pos[comp_id])
        assert bought >= dec.requirement.to_buy
        assert sum(q for p in pos[comp_id] for _, q in p.serves) == dec.requirement.to_buy
        for p in pos[comp_id]:
            entry = catalog[(p.supplier_id, comp_id)]
            opt = next(o for o in dec.options if o.supplier.supplier_id == p.supplier_id and o.mode == p.mode)
            assert opt.eligible, (comp_id, p.supplier_id)
            assert state.suppliers[p.supplier_id].on_approved_list
            assert p.unit_price == entry.unit_price
            assert p.quantity >= entry.minimum_order_qty and isinstance(p.quantity, int)
            assert p.order_date == state.current_date
            assert p.expected_delivery_date == p.order_date + timedelta(days=opt.lead_days)
            if p.mode == "standard":
                assert opt.lead_days == entry.lead_time_days
            assert (p.supplier_id, p.mode) in dec.choice_reasons
            if not opt.domestic:
                assert dec.intl_reasons.get((p.supplier_id, p.mode))


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_deterministic(scenario_copy, name):
    a = plan(scenario_copy, name)[2]
    b = plan(scenario_copy, name)[2]
    flat = lambda pos: sorted((p.component_id, p.supplier_id, p.mode, p.quantity) for ps in pos.values() for p in ps)
    assert flat(a) == flat(b)
