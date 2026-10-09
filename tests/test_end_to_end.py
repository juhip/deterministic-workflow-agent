"""Step 5: double-check, alerts, rationale, writing - and the whole agent end to end."""
import re
import sqlite3
from datetime import date

import pytest

from conftest import ALL_SCENARIOS
import agent
from procurement.db import load_state
from procurement.pipeline import run
from procurement.rules import Rulebook

RULES = Rulebook.load()


def result_for(scenario_copy, name):
    return run(load_state(scenario_copy(name)), RULES)


def status(res, order_id):
    return next(s for s in res.statuses if s.order.order_id == order_id)


def texts(res, severity=None):
    return [a.text for a in res.alerts if severity is None or a.severity == severity]


# ---- the independent recount (replay) -------------------------------------------------
def test_s01_bayline_one_day_late_because_of_magnets(scenario_copy):
    res = result_for(scenario_copy, "scenario_01_baseline.sqlite")
    st = status(res, "WO-7101")
    assert st.late_days == 1 and [c for c, _ in st.limiting] == ["PT-103"]
    assert st.buildable_on_time == 15                      # 90 magnets in stock / 6 per pump
    assert all(status(res, o).on_time for o in ("WO-7102", "WO-7103", "WO-7104"))


def test_s03_rush_order_one_day_late(scenario_copy):
    res = result_for(scenario_copy, "scenario_03_tight_timeline.sqlite")
    st = status(res, "WO-7105")
    assert st.late_days == 1
    assert {c for c, _ in st.limiting} == {"PT-105", "PT-117"}   # choke cores arrive in time by air freight
    assert st.buildable_on_time == 18                      # 36 circuit boards in stock / 2 per unit


def test_s03_air_freight_used_inside_its_window(scenario_copy):
    """MEMO-2026-044 is in force on 2026-10-05: the rush order's choke cores fly in from the
    Netherlands because no domestic supplier can make the date, held for the Sourcing Manager."""
    res = result_for(scenario_copy, "scenario_03_tight_timeline.sqlite")
    air = [(r, p) for r, p in zip(res.releases, res.pos) if p.mode == "air"]
    assert [(p.component_id, p.supplier_id) for _, p in air] == [("PT-118", "VEN-304")]
    r, p = air[0]
    assert r.needs_approval and r.approver == "Sourcing Manager" and "MEMO-2026-044" in p.rationale


def test_s05_only_ostrander_late_and_alert_explains_the_rule(scenario_copy):
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    assert status(res, "WO-7101").on_time                 # Bayline gets Ironbridge's magnets first
    assert status(res, "WO-7102").late_days == 5
    crit = " ".join(texts(res, "CRITICAL"))
    assert "WO-7102" in crit and "followed MEMO-2026-018" in crit
    assert any("Decision needed" in t and "5 to 0 days" in t for t in texts(res, "ACTION"))
    assert any("Air-freight authorization" in t and "ended" in t for t in texts(res, "INFO"))


def test_s06_everything_on_time_and_quiet(scenario_copy):
    res = result_for(scenario_copy, "scenario_06_simple.sqlite")
    assert len(res.pos) == 2
    assert all(s.on_time for s in res.statuses)
    assert texts(res, "CRITICAL") == []


# ---- alerts that must appear ----------------------------------------------------------
def test_s01_expected_alerts(scenario_copy):
    res = result_for(scenario_copy, "scenario_01_baseline.sqlite")
    all_text = " ".join(texts(res))
    assert "Epoxy Potting Resin" in all_text and "HAZMAT" in all_text
    assert "ITM-4403" in all_text and "ITM-4405" in all_text
    assert "Mexico" in all_text
    assert any("Single-supplier risk for Circuit Board" in t for t in texts(res, "WARNING"))
    assert any("rated C" in t for t in texts(res, "ACTION"))


def test_s02_existing_orders_conflict_reported(scenario_copy):
    res = result_for(scenario_copy, "scenario_02_partial_procurement.sqlite")
    assert any("65%" in t for t in texts(res, "WARNING"))


def test_approval_threshold_flagged(scenario_copy, tmp_path):
    # inflate demand so one order exceeds $40,000 and check the approval flag appears
    path = scenario_copy("scenario_06_simple.sqlite")
    con = sqlite3.connect(path)
    con.execute("UPDATE production_schedule SET quantity = 600")
    con.commit(); con.close()
    res = run(load_state(path), RULES)
    assert any("Sourcing Manager" in t or "VP of Supply Chain" in t for t in texts(res, "ACTION"))


# ---- rationale ------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_every_order_explains_itself(scenario_copy, name):
    res = result_for(scenario_copy, name)
    for p in res.pos:
        r = p.rationale
        assert r.startswith(("DRAFT, awaiting approval by ", "RELEASED automatically"))
        assert f" Buy {p.quantity} " in r
        assert "Need:" in r and "Why this supplier:" in r
        if not next(o for o in res.decisions[p.component_id].options
                    if o.supplier.supplier_id == p.supplier_id and o.mode == p.mode).domestic:
            assert "International sourcing justification" in r


# ---- writing to the database, end to end ------------------------------------------------
def rows(path, sql):
    con = sqlite3.connect(path)
    out = con.execute(sql).fetchall()
    con.close()
    return out


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_agent_cli_end_to_end(scenario_copy, name):
    path = scenario_copy(name)
    before = rows(path, "SELECT * FROM purchase_orders ORDER BY po_number")
    assert agent.main(["--scenario", str(path), "--quiet"]) == 0
    after = rows(path, "SELECT * FROM purchase_orders WHERE po_number NOT LIKE 'AGT-%' ORDER BY po_number")
    assert after == before                                   # pre-existing orders untouched
    new = rows(path, "SELECT po_number, quantity, unit_price, order_date, expected_delivery_date, rationale "
                     "FROM purchase_orders WHERE po_number LIKE 'AGT-%'")
    assert new and all(r[5] for r in new)
    alerts = rows(path, "SELECT description FROM alerts")
    assert alerts and all(a[0].startswith("[") for a in alerts)
    assert alerts[-1][0].startswith("[INFO] Run summary")


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_run_keeps_the_fixed_schema(scenario_copy, name):
    """The data spec fixes the purchase_orders and alerts columns, and held-out databases will have
    exactly those. Held vs. released is written into the rationale, never into a new column."""
    path = scenario_copy(name)
    schema = "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    before = rows(path, schema)
    assert agent.main(["--scenario", str(path), "--quiet"]) == 0
    assert rows(path, schema) == before                      # no table, column or index added
    assert [r[1] for r in rows(path, "PRAGMA table_info(purchase_orders)")] == [
        "po_number", "component_id", "supplier_id", "quantity", "unit_price",
        "order_date", "expected_delivery_date", "rationale"]
    for (why,) in rows(path, "SELECT rationale FROM purchase_orders WHERE po_number LIKE 'AGT-%'"):
        assert why.startswith(("DRAFT, awaiting approval by ", "RELEASED automatically"))


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_second_run_sees_its_own_orders_and_buys_nothing_more(scenario_copy, name):
    """The agent loop: placed orders are commitments; the next run reads them back as supply."""
    path = scenario_copy(name)
    agent.main(["--scenario", str(path), "--quiet"])
    first = rows(path, "SELECT * FROM purchase_orders ORDER BY po_number")
    late = lambda: sorted(re.findall(r"start date for (WO-\d+).*?Materials complete (\S+)",
                                     " ".join(r[0] for r in rows(path, "SELECT description FROM alerts"))))
    first_late = late()
    agent.main(["--scenario", str(path), "--quiet"])
    assert rows(path, "SELECT * FROM purchase_orders ORDER BY po_number") == first      # nothing deleted or duplicated
    assert late() == first_late                                                          # same orders late, same dates
    summary = rows(path, "SELECT description FROM alerts ORDER BY alert_id DESC LIMIT 1")[0][0]
    assert "0 purchase orders" in summary


def test_new_orders_numbered_after_earlier_ones(scenario_copy):
    path = scenario_copy("scenario_06_simple.sqlite")
    agent.main(["--scenario", str(path), "--quiet"])
    con = sqlite3.connect(path)
    con.execute("UPDATE production_schedule SET quantity = quantity + 20")   # demand grows between runs
    con.commit(); con.close()
    agent.main(["--scenario", str(path), "--quiet"])
    nums = [r[0] for r in rows(path, "SELECT po_number FROM purchase_orders ORDER BY po_number")]
    assert nums[:2] == ["AGT-0001", "AGT-0002"] and len(nums) > 2 and len(set(nums)) == len(nums)


def test_dry_run_writes_nothing(scenario_copy):
    path = scenario_copy("scenario_01_baseline.sqlite")
    agent.main(["--scenario", str(path), "--quiet", "--dry-run"])
    assert rows(path, "SELECT COUNT(*) FROM purchase_orders")[0][0] == 0
    assert rows(path, "SELECT COUNT(*) FROM alerts")[0][0] == 0


def test_validator_blocks_a_bad_order(scenario_copy):
    """If the decider ever produced an illegal order, the double-check must stop it."""
    from procurement.validator import hard_violations
    from procurement.decider import PlannedPO
    state = load_state(scenario_copy("scenario_01_baseline.sqlite"))
    res = run(state, RULES)
    bad = PlannedPO("PT-105", "VEN-313", 100, 5.4, state.current_date, date(2026, 10, 22), "standard")
    reasons = [r for _, r in hard_violations(state, RULES, res.tags, [bad])]
    assert any("Approved Supplier List" in r for r in reasons)


# ---- approve by exception (default) ---------------------------------------------------------
def released(res):
    return [p for r, p in zip(res.releases, res.pos) if not r.needs_approval]


def held(res):
    return [p for r, p in zip(res.releases, res.pos) if r.needs_approval]


def test_s06_routine_order_released_critical_part_held(scenario_copy):
    res = result_for(scenario_copy, "scenario_06_simple.sqlite")
    assert [p.component_id for p in released(res)] == ["PT-116"]   # probe housing: routine, domestic, $264
    assert [p.component_id for p in held(res)] == ["PT-114"]       # level transducer: critical part
    assert held(res)[0].rationale.startswith("DRAFT, awaiting approval by Buyer on duty")
    assert released(res)[0].rationale.startswith("RELEASED automatically")


def test_s05_magnets_held_with_all_reasons_and_queue_is_urgent_first(scenario_copy):
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    halong = next(r for r, p in zip(res.releases, res.pos) if p.supplier_id == "VEN-307")
    joined = " ".join(halong.reasons)
    for expected in ("critical part", "MEMO-2026-018", "international supplier", "rated C", "day(s) after", "conflict"):
        assert expected in joined, expected
    queue = [t for t in texts(res, "ACTION") if t.startswith("Approval needed from Sourcing Manager")]
    assert len(queue) == 1 and "(1) Samarium-Cobalt Magnet Segment from Halong" in queue[0]   # the late order is listed first
    assert any("released automatically" in t for t in texts(res, "INFO"))


def test_approve_every_order_mode(scenario_copy):
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["order_release"] = "approve_every_order"
    res = run(load_state(scenario_copy("scenario_01_baseline.sqlite")), Rulebook(raw))
    assert res.pos and all(r.needs_approval for r in res.releases)
    assert "0 released automatically" in res.alerts[-1].text


def test_policy_thresholds_mode_releases_routine_and_risky_alike(scenario_copy):
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["order_release"] = "policy_thresholds_only"
    res = run(load_state(scenario_copy("scenario_05_competing_demand.sqlite")), Rulebook(raw))
    held = [r for r in res.releases if r.needs_approval]
    # nothing is over $40k or air freight; only the policy's own section 9 (Strategic shift) still applies
    assert held and all(all(x.startswith("moves volume away from") for x in r.reasons) for r in held)
    assert len(held) < len(res.releases)


def test_exceptions_are_tunable(scenario_copy):
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["approval_exceptions"]["critical_part"] = False
    res = run(load_state(scenario_copy("scenario_06_simple.sqlite")), Rulebook(raw))
    assert held(res) == []                                         # transducer now routine


def test_critical_hold_can_be_limited_to_single_sourced_parts(scenario_copy):
    """Narrower mode: a critical part with a second allowed supplier releases; a single-sourced one stays held."""
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["approval_exceptions"]["critical_part"] = "single_sourced_only"
    assert held(run(load_state(scenario_copy("scenario_06_simple.sqlite")), Rulebook(raw))) == []
    state = load_state(scenario_copy("scenario_05_competing_demand.sqlite"))
    res = run(state, Rulebook(raw))
    boards = [r for po, r in zip(res.pos, res.releases) if "Circuit Board" in state.components[po.component_id].name]
    assert boards and all(r.needs_approval and any(x.startswith("critical part") for x in r.reasons) for r in boards)


def test_single_supplier_hold_can_be_limited_to_critical_parts(scenario_copy):
    """Section 4's two-supplier rule covers critical parts only; a non-critical single-sourced order releases."""
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["approval_exceptions"]["single_allowed_supplier"] = "critical_only"
    state = load_state(scenario_copy("scenario_04_low_inventory.sqlite"))
    res = run(state, Rulebook(raw))
    for po, r in zip(res.pos, res.releases):
        if "only one supplier is allowed for this part" in r.reasons:
            assert res.tags[po.component_id].critical
    caps = [r for po, r in zip(res.pos, res.releases) if state.components[po.component_id].name == "DC Link Capacitor Pack"]
    assert caps and not any(r.needs_approval for r in caps)


def test_large_order_routes_to_the_right_approver(scenario_copy):
    path = scenario_copy("scenario_06_simple.sqlite")
    con = sqlite3.connect(path)
    con.execute("UPDATE production_schedule SET quantity = 600")
    con.commit(); con.close()
    res = run(load_state(path), RULES)
    assert any(r.approver in ("Sourcing Manager", "VP of Supply Chain") for r in res.releases)


def test_s05_conflict_goes_to_procurement_manager(scenario_copy):
    """Following the magnet memo makes Ostrander late: the decision request and the held magnet
    orders go to the Sourcing Manager (a rulebook setting), not the VP."""
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    decision = [t for t in texts(res, "ACTION") if t.startswith("Decision needed")]
    assert decision and all("The Sourcing Manager should confirm" in t for t in decision)
    magnets = [r for r, p in zip(res.releases, res.pos) if p.component_id == "PT-103"]
    assert magnets and all(r.approver == "Sourcing Manager" for r in magnets)


def test_conflict_decider_is_a_setting(scenario_copy):
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["conflict_decided_by"] = "VP of Supply Chain"
    res = run(load_state(scenario_copy("scenario_05_competing_demand.sqlite")), Rulebook(raw))
    assert any("The VP of Supply Chain should confirm" in t for t in texts(res, "ACTION"))
    assert all(r.approver == "VP of Supply Chain"
               for r, p in zip(res.releases, res.pos) if p.component_id == "PT-103")


def test_shift_away_from_strategic_supplier_needs_vp(scenario_copy):
    """Policy section 9: Fathom (Strategic) could deliver the sensors on time, but a cheaper
    supplier was chosen, so the order goes to the VP of Supply Chain."""
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    sensors = [(r, p) for r, p in zip(res.releases, res.pos) if p.component_id in ("PT-113", "PT-115")]
    assert sensors and all(p.supplier_id != "VEN-312" for _, p in sensors)
    for r, _ in sensors:
        assert r.approver == "VP of Supply Chain"
        assert any("away from Strategic supplier Fathom Instruments" in x for x in r.reasons)


def test_forced_shift_is_not_escalated(scenario_copy):
    """When the Strategic supplier cannot make the date, moving away is not a choice: no VP hold."""
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    housing = next(r for r, p in zip(res.releases, res.pos) if p.component_id == "PT-108")
    assert not any(x.startswith("moves volume away from") for x in housing.reasons)


def test_significant_shift_threshold_is_a_setting(scenario_copy):
    import copy
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["strategic_shift_significant_above"] = 1000
    res = run(load_state(scenario_copy("scenario_05_competing_demand.sqlite")), Rulebook(raw))
    assert not any(any(x.startswith("moves volume away from") for x in r.reasons) for r in res.releases)


def test_late_order_alert_states_revenue_at_risk(scenario_copy):
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    late = [t for t in texts(res, "CRITICAL") if "WO-7102" in t]
    assert late and "Revenue at risk: 18 x TideRunner 90 at $3,860 = $69,480" in late[0]


def test_air_freight_cost_estimated_only_for_parts_bought_by_weight(scenario_copy):
    from types import SimpleNamespace
    from procurement.alerts import air_freight_cost
    state = load_state(scenario_copy("scenario_04_low_inventory.sqlite"))     # 2026-10-05: air memo in force
    kg = air_freight_cost(SimpleNamespace(component_id="PT-101", quantity=100), state, RULES)   # winding wire, kg
    each = air_freight_cost(SimpleNamespace(component_id="PT-113", quantity=100), state, RULES)  # sensors, each
    assert "$600-$900" in kg and "MEMO-2026-044" in kg
    assert "not computable" in each


# ---- policy clauses the six samples never trigger -----------------------------------------
def _changed_s6(scenario_copy, *sql):
    path = scenario_copy("scenario_06_simple.sqlite")
    con = sqlite3.connect(path)
    for s in sql:
        con.execute(s)
    con.commit(); con.close()
    return run(load_state(path), RULES)


def test_sole_source_part_carries_justification(scenario_copy):
    """Policy section 5: a part with only one supplier must carry a Sole Source Justification."""
    res = _changed_s6(scenario_copy, "DELETE FROM supplier_catalog WHERE component_id='PT-116' AND supplier_id != 'VEN-302'")
    housing = next(p for p in res.pos if p.component_id == "PT-116")
    assert "Sole Source Justification required" in housing.rationale


def test_order_over_40k_goes_to_sourcing_manager(scenario_copy):
    """Policy section 7: over $40,000 needs the Sourcing Manager."""
    res = _changed_s6(scenario_copy, "UPDATE production_schedule SET quantity = 600")
    big = [(r, p) for r, p in zip(res.releases, res.pos) if p.total > 40000]
    assert big and all(r.approver in ("Sourcing Manager", "VP of Supply Chain") for r, _ in big)
    assert all("exceeds $40,000" in p.rationale for _, p in big)


def test_emergency_order_flagged_for_retroactive_approval(scenario_copy):
    """Policy section 7.1: an order that prevents a stoppage, up to $60,000, is flagged for
    retroactive Sourcing Manager approval. The MVP still holds it (the cautious reading)."""
    res = _changed_s6(scenario_copy, "UPDATE production_schedule SET quantity = 600, materials_needed_by = '2026-10-12'")
    r, p = next((r, p) for r, p in zip(res.releases, res.pos) if 40000 < p.total <= 60000)
    assert "EMERGENCY ORDER" in p.rationale and "retroactive Sourcing Manager approval" in p.rationale
    assert r.needs_approval and r.approver == "Sourcing Manager"


def test_alerts_are_addressed_to_the_users_named_in_the_rulebook(scenario_copy):
    """Each recipient (approvers, receiving team) gets its alerts by the name set in the rulebook."""
    res = result_for(scenario_copy, "scenario_05_competing_demand.sqlite")
    who = RULES.raw["settings"]
    texts = [a.text for a in res.alerts]
    assert any(t.startswith(f"{who['receiving_team']}: RECEIVING") for t in texts)          # Certificate of Conformance
    assert any("HAZMAT" in t and f"{who['receiving_team']}: receive into Hazmat Storage" in t for t in texts)
    assert any(p.rationale.startswith(f"DRAFT, awaiting approval by {who['default_approver']}") for p in held(res))
