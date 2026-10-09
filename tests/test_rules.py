"""Rulebook, classifier and supplier-option tests."""
import copy
import re
from datetime import date
from pathlib import Path

import pytest

from conftest import ALL_SCENARIOS, PROJECT
from procurement.classifier import classify_all
from procurement.db import load_state
from procurement.options import build_options, is_domestic
from procurement.rules import Rulebook, RulebookError

RULES = Rulebook.load()


def eligible_ids(opts):
    return {o.supplier.supplier_id for o in opts if o.eligible and o.mode == "standard"}


def setup(scenario_copy, name="scenario_01_baseline.sqlite"):
    state = load_state(scenario_copy(name))
    return state, classify_all(state.components, RULES, state.current_date)


# ---- rulebook file ---------------------------------------------------------------------
def test_rulebook_loads_and_every_rule_cites_a_known_document():
    docs = {d["id"] for d in RULES.raw["documents"]}
    assert RULES.rules
    for r in RULES.rules:
        assert r.source["document"] in docs, r.id
        assert r.source.get("quote"), r.id


def test_rulebook_rejects_mistakes():
    bad = copy.deepcopy(RULES.raw)
    bad["rules"][0]["type"] = "made_up_rule"
    with pytest.raises(RulebookError, match="unknown type"):
        Rulebook(bad)
    bad = copy.deepcopy(RULES.raw)
    bad["rules"][0]["effective_to"] = "2000-01-01"
    with pytest.raises(RulebookError, match="before"):
        Rulebook(bad)
    bad = copy.deepcopy(RULES.raw)
    bad["rules"][0]["scope"]["selector"] = {"typo_key": 1}
    with pytest.raises(RulebookError, match="selector"):
        Rulebook(bad)


def test_air_freight_window_is_inclusive():
    on = lambda d: bool(RULES.active("expedited_shipping", date.fromisoformat(d)))
    assert not on("2026-08-02") and on("2026-08-03") and on("2026-11-02") and not on("2026-11-03")


def test_memo_overrides_policy_for_magnets(scenario_copy):
    state, _ = setup(scenario_copy)
    magnets = state.components["PT-103"]
    caps = RULES.for_component("concentration_limit", magnets, state.current_date)
    assert [r.id for r in caps] == ["concentration-magnets"]
    assert caps[0].params["max_share"] == 0.6
    # before the memo existed, the general policy applies
    early = RULES.for_component("concentration_limit", magnets, date(2026, 3, 2))
    assert [r.id for r in early] == ["concentration-default"]


# ---- classifier ------------------------------------------------------------------------
def test_critical_components(scenario_copy):
    _, tags = setup(scenario_copy)
    critical = {c for c, t in tags.items() if t.critical}
    # controller IC, IGBT, circuit board, magnets, flow/level/salinity sensors; NOT the probe housing
    assert critical == {"PT-103", "PT-105", "PT-106", "PT-107", "PT-113", "PT-114", "PT-115"}
    assert "board assembly" in tags["PT-105"].critical_note


def test_required_certifications(scenario_copy):
    _, tags = setup(scenario_copy)
    assert set(tags["PT-117"].required_certs) == {"ISO-9001", "IEC-62368"}   # capacitor pack
    assert set(tags["PT-118"].required_certs) == {"ISO-9001", "IEC-62368"}   # choke core
    assert set(tags["PT-112"].required_certs) == {"ISO-9001"}                # electronic category
    assert tags["PT-101"].required_certs == {}                               # winding wire
    assert tags["PT-103"].required_certs == {}                               # magnets are raw material


def test_memo_id_mismatch_is_recorded(scenario_copy):
    _, tags = setup(scenario_copy)
    assert any("ITM-4403" in n for n in tags["PT-103"].alias_notes)
    assert any("ITM-4405" in n for n in tags["PT-105"].alias_notes)
    assert tags["PT-101"].alias_notes == []


# ---- supplier options ------------------------------------------------------------------
def test_banned_supplier_never_eligible(scenario_copy):
    state, tags = setup(scenario_copy)
    opts = build_options(state, RULES, tags["PT-105"], "PT-105")
    sup113 = [o for o in opts if o.supplier.supplier_id == "VEN-313"]
    assert sup113 and all(not o.eligible for o in sup113)
    assert any("Approved Supplier List" in r for r in sup113[0].excluded_because)


def test_iec_requirement_filters_suppliers(scenario_copy):
    state, tags = setup(scenario_copy)
    assert eligible_ids(build_options(state, RULES, tags["PT-117"], "PT-117")) == {"VEN-301"}
    assert eligible_ids(build_options(state, RULES, tags["PT-118"], "PT-118")) == {"VEN-301", "VEN-304"}  # both certified


def test_pcb_freeze_keeps_only_established_supplier(scenario_copy):
    state, tags = setup(scenario_copy)
    opts = build_options(state, RULES, tags["PT-105"], "PT-105")
    assert eligible_ids(opts) == {"VEN-301"}
    eco = next(o for o in opts if o.supplier.supplier_id == "VEN-310")
    assert any("freeze" in r for r in eco.excluded_because)


def test_pcb_freeze_not_applied_before_memo_date(scenario_copy):
    state, _ = setup(scenario_copy)
    state.current_date = date(2026, 8, 1)
    tags = classify_all(state.components, RULES, state.current_date)
    # VEN-303 (cheap, Standard tier) is allowed again; VEN-313 stays banned
    assert eligible_ids(build_options(state, RULES, tags["PT-105"], "PT-105")) == {"VEN-301", "VEN-303", "VEN-310"}


def test_mexico_is_domestic_per_policy(scenario_copy):
    state, _ = setup(scenario_copy)
    assert is_domestic(state.suppliers["VEN-310"], RULES, state.current_date) is True
    assert is_domestic(state.suppliers["VEN-307"], RULES, state.current_date) is False


def test_delivery_dates_and_air_freight(scenario_copy):
    state, tags = setup(scenario_copy)
    opts = {(o.supplier.supplier_id, o.mode): o for o in build_options(state, RULES, tags["PT-103"], "PT-103")}
    assert opts[("VEN-308", "standard")].arrival == date(2026, 10, 17)     # 12 days
    assert opts[("VEN-307", "standard")].arrival == date(2026, 11, 4)      # 30 days by sea
    assert opts[("VEN-307", "air")].lead_days == 20                        # 30 - 10
    assert opts[("VEN-307", "air")].flags
    assert ("VEN-308", "air") not in opts                                  # domestic: no air option


def test_air_floor_of_five_days(scenario_copy):
    state, tags = setup(scenario_copy)
    opts = build_options(state, RULES, tags["PT-109"], "PT-109")
    air = next(o for o in opts if o.supplier.supplier_id == "VEN-304" and o.mode == "air")
    assert air.lead_days == 5  # 12 - 10 = 2, raised to the 5-day floor


def test_no_air_freight_after_memo_expired(scenario_copy):
    state, tags = setup(scenario_copy, "scenario_05_competing_demand.sqlite")
    for comp_id in state.components:
        assert all(o.mode == "standard" for o in build_options(state, RULES, tags[comp_id], comp_id))


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_every_short_component_has_an_eligible_supplier(scenario_copy, name):
    state, tags = setup(scenario_copy, name)
    for comp_id in state.components:
        assert eligible_ids(build_options(state, RULES, tags[comp_id], comp_id)), comp_id


# ---- guard rail: no scenario-specific facts in the code --------------------------------
def test_no_hardcoded_ids_or_dates_in_logic():
    pattern = re.compile(r"\bPT-\d|VEN-\d|MEMO-\d|ITM-\d|\b20\d\d-\d\d-\d\d\b")
    offenders = []
    for f in (PROJECT / "procurement").glob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if pattern.search(line.split("#")[0]):
                offenders.append(f"{f.name}:{n}: {line.strip()}")
    assert offenders == [], "IDs/dates belong in the rulebook or database, not code:\n" + "\n".join(offenders)


def test_coating_described_as_board_spray_is_not_a_board(scenario_copy):
    """Regression: 'Marine Conformal Varnish - protective spray for circuit boards' must not inherit board rules."""
    state, tags = setup(scenario_copy)
    assert not tags["PT-111"].critical
    assert "pcb-supplier-freeze" not in tags["PT-111"].rule_ids
    assert eligible_ids(build_options(state, RULES, tags["PT-111"], "PT-111")) == {"VEN-301", "VEN-306"}


def _code_strings(path):
    """Every string literal in a module, except docstrings and f-string format specs."""
    import ast
    tree = ast.parse(path.read_text())
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    specs = {id(c) for n in ast.walk(tree) if isinstance(n, ast.FormattedValue) and n.format_spec
             for c in ast.walk(n.format_spec)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs | specs:
            yield n.lineno, n.value


def test_no_policy_titles_or_numbers_in_messages():
    """Approver titles, percentages, dollar amounts and time windows come from the rulebook, so a
    policy change updates every decision AND every message. None may be typed into the code."""
    import json
    raw = json.loads((PROJECT / "rules" / "policy_rules.json").read_text())
    titles = set()
    for r in raw["rules"]:
        p = r["params"]
        titles |= {l["approver"] for l in p.get("approval_levels", [])}
        titles |= {p[k] for k in ("approver", "shift_away_approver", "emergency_retroactive_approver") if p.get(k)}
    titles |= {raw["settings"][k] for k in ("default_approver", "conflict_decided_by", "receiving_team")}
    banned = re.compile("|".join([re.escape(t) for t in titles] + [r"\d+\s?%", r"\$\s?\d", r"\b\d+[- ]months?\b"]))
    offenders = [f"{f.name}:{line}: {text!r}" for f in sorted((PROJECT / "procurement").glob("*.py"))
                 if f.name != "llm.py" for line, text in _code_strings(f) if banned.search(text)]
    assert offenders == [], "Policy titles/numbers belong in the rulebook:\n" + "\n".join(offenders)
