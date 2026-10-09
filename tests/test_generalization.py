"""Held-out-style scenarios: new ID formats (e.g. JOB-6030) and new dates,
including dates before the policy took effect. Plus the per-run assumptions alert."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import gen_scenarios as gen                        # noqa: E402
import policy_scorecard as ps                      # noqa: E402
from procurement.db import load_state              # noqa: E402
from procurement.messages import check             # noqa: E402
from procurement.pipeline import run               # noqa: E402
from procurement.rules import Rulebook             # noqa: E402

RULES = Rulebook.load()


@pytest.fixture(autouse=True)
def fresh_checks():
    for c in ps.CHECKS.values():
        c.checked, c.failed = 0, []


def variant(tmp_path, name, today):
    return gen.make_variant(gen.DATA / name, tmp_path / name, today)


@pytest.mark.parametrize("name,today,why", gen.VARIANTS, ids=[v[2] for v in gen.VARIANTS])
def test_generated_scenario_passes_every_policy_check(tmp_path, name, today, why):
    ps.grade(variant(tmp_path, name, today), why, RULES)
    assert {k: c.failed for k, c in ps.CHECKS.items() if c.failed} == {}


def test_before_the_policy_took_effect_policy_still_applies(tmp_path):
    state = load_state(variant(tmp_path, "scenario_04_low_inventory.sqlite", "2025-12-14"))
    res = run(state, RULES)
    assert any(a.severity == "WARNING" and "before HF-PUR-100 took effect" in a.text for a in res.alerts)
    banned = {s.supplier_id for s in state.suppliers.values() if not s.on_approved_list}
    assert banned and not any(p.supplier_id in banned for p in res.pos)       # approved list still enforced


def test_memo_part_numbers_that_match_the_data_need_no_name_matching(tmp_path):
    res = run(load_state(variant(tmp_path, "scenario_01_baseline.sqlite", None)), RULES)
    assert any(p.component_id == "ITM-4403" for p in res.pos)
    assert not any("Document mismatch" in a.text for a in res.alerts)
    assert any(a.text.startswith("Recovery options for JOB-6030") for a in res.alerts)


def test_fact_check_keeps_order_numbers_in_any_format():
    names = {"JOB-6030", "Bayline Marine"}
    original = "Cannot meet 2026-05-20 start date for JOB-6030 (Bayline Marine): 3 day(s) late."
    assert check(original, "Bayline Marine (JOB-6030) is 3 days late for 2026-05-20.", names) is None
    assert "JOB-6030" in check(original, "Bayline Marine is 3 days late for 2026-05-20.", names)


def test_assumptions_alert_lists_only_what_shaped_this_run(scenario_copy):
    s5 = run(load_state(scenario_copy("scenario_05_competing_demand.sqlite")), RULES)
    text = next(a.text for a in s5.alerts if a.text.startswith("Assumptions this run relied on"))
    assert "earliest need-by date first" in text            # orders compete for shared parts
    assert "hazardous-material receiving" in text           # hazmat parts were bought
    assert "Strategic supplier counts as 'significant'" in text
    assert "air freight" not in text                         # no air freight used on 2026-11-09
    s6 = run(load_state(scenario_copy("scenario_06_simple.sqlite")), RULES)
    text6 = next(a.text for a in s6.alerts if a.text.startswith("Assumptions this run relied on"))
    assert "earliest need-by" not in text6 and "Supplier capacity is treated as unlimited" in text6


@pytest.mark.parametrize("name,today,why,mutate", gen.STRUCTURAL, ids=[v[2][:40] for v in gen.STRUCTURAL])
def test_structurally_changed_scenario_passes_every_policy_check(tmp_path, name, today, why, mutate):
    ps.grade(gen.make_variant(gen.DATA / name, tmp_path / name, today, mutate), why, RULES)
    assert {k: c.failed for k, c in ps.CHECKS.items() if c.failed} == {}


def test_scaled_demand_reaches_the_policy_approval_levels(tmp_path):
    name, today, why, mutate = gen.STRUCTURAL[0]
    ps.grade(gen.make_variant(gen.DATA / name, tmp_path / name, today, mutate), why, RULES)
    assert ps.CHECKS["thresholds"].checked > 0          # orders over $40,000 / $120,000 were actually graded


def test_baseline_breaks_rules_the_agent_keeps(scenario_copy):
    import baseline
    state = load_state(scenario_copy("scenario_05_competing_demand.sqlite"))
    naive, agent = baseline.naive_plan(state), run(state, RULES).pos
    assert baseline.hard_rule_breaks(state, RULES, naive)          # policy-blind planner breaks hard rules
    assert baseline.hard_rule_breaks(state, RULES, agent) == []    # the agent breaks none of them


def test_tiny_magnet_demand_still_respects_minimum_orders(tmp_path):
    """Stress test finding: at small demand the magnet split returned an order below the
    supplier's minimum, the validation gate blocked it, and nothing was bought."""
    import stress_test as st
    path = st.build(gen.DATA / "scenario_04_low_inventory.sqlite", None, [st.scale_demand(0.2)], tmp_path / "s4.sqlite")
    state = load_state(path)
    res = run(state, RULES)
    assert not any("Blocked a planned order" in a.text for a in res.alerts)
    moq = {(e.supplier_id, e.component_id): e.minimum_order_qty for e in state.catalog}
    assert all(p.quantity >= moq[(p.supplier_id, p.component_id)] for p in res.pos)
    ps.grade(path, "tiny magnet demand", RULES)
    assert {k: c.failed for k, c in ps.CHECKS.items() if c.failed} == {}


def test_a_part_nobody_sells_still_gets_recovery_options(tmp_path):
    import stress_test as st
    path = st.build(gen.DATA / "scenario_01_baseline.sqlite", None, [st.unbuyable_part], tmp_path / "s1.sqlite")
    res = run(load_state(path), RULES)
    late = [s.order.order_id for s in res.statuses if not s.on_time]
    options = [a.text for a in res.alerts if a.text.startswith("Recovery options")]
    assert late and all(any(t.startswith(f"Recovery options for {oid}") for t in options) for oid in late)
    assert any("from a new supplier" in t for t in options)
