import hashlib
import json
import sqlite3
from datetime import date

import pytest

from conftest import ALL_SCENARIOS, SCENARIO_DIR
from procurement.db import load_state, parse_date, split_list


def test_current_date_trap_is_real(scenario_copy):
    """Regression guard: unquoted current_date returns the machine's date, not the scenario's."""
    path = scenario_copy("scenario_01_baseline.sqlite")
    con = sqlite3.connect(path)
    unquoted = con.execute("SELECT current_date FROM scenario_config").fetchone()[0]
    quoted = con.execute('SELECT "current_date" FROM scenario_config').fetchone()[0]
    con.close()
    assert quoted == "2026-10-05"
    assert unquoted != quoted  # proves the trap exists, so the quoting in db.py matters


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_loader_matches_manifest_date(scenario_copy, name):
    manifest = {m["file"]: m["current_date"] for m in json.loads((SCENARIO_DIR / "manifest.json").read_text())}
    state = load_state(scenario_copy(name))
    assert state.current_date.isoformat() == manifest[name]


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_loader_reads_everything_cleanly(scenario_copy, name):
    s = load_state(scenario_copy(name))
    assert s.schedule and s.components and s.suppliers and s.catalog
    assert s.issues == []
    assert s.existing_alert_count == 0
    # schedule is sorted earliest-deadline first (the netting layer relies on this)
    keys = [(o.materials_needed_by, o.order_id) for o in s.schedule]
    assert keys == sorted(keys)


def test_loader_types(scenario_copy):
    s = load_state(scenario_copy("scenario_01_baseline.sqlite"))
    assert isinstance(s.current_date, date)
    assert s.bom["ASM-210"]["PT-111"] == 0.25            # fractional BOM quantity kept
    assert s.components["PT-110"].is_hazardous is True
    assert s.components["PT-105"].requires_certification == ("ISO-9001",)
    assert s.components["PT-101"].requires_certification == ()   # NULL column -> empty
    assert s.suppliers["VEN-301"].certifications == ("ISO-9001", "IEC-62368")
    assert s.suppliers["VEN-313"].on_approved_list is False
    assert s.suppliers["VEN-310"].country == "Mexico" and s.suppliers["VEN-310"].is_domestic is False


def test_existing_pos_loaded_in_scenario_02(scenario_copy):
    s = load_state(scenario_copy("scenario_02_partial_procurement.sqlite"))
    assert {p.po_number for p in s.existing_pos} == {"PRIOR-001", "PRIOR-002", "PRIOR-003", "PRIOR-004"}
    magnets = [p for p in s.existing_pos if p.component_id == "PT-103"]
    assert sum(p.quantity for p in magnets) == 140
    assert all(p.expected_delivery_date is not None for p in s.existing_pos)


def test_loading_never_modifies_the_file(scenario_copy):
    path = scenario_copy("scenario_01_baseline.sqlite")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    load_state(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_original_data_untouched_by_tests():
    """No test writes to the provided data: every file is byte-for-byte what it was when the run began.
    (Someone may have run agent.py on a sample before the tests; the evaluations remove that run's
    rows from their copies, so the samples only need to be untouched by the tests themselves.)"""
    from conftest import DATA_FINGERPRINTS
    for name, fingerprint in DATA_FINGERPRINTS.items():
        assert hashlib.sha256((SCENARIO_DIR / name).read_bytes()).hexdigest() == fingerprint, name


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_state(tmp_path / "nope.sqlite")


def test_helpers():
    assert parse_date("2026-10-05") == date(2026, 10, 5)
    assert parse_date("2026-10-05 08:00:00") == date(2026, 10, 5)
    assert parse_date("") is None and parse_date(None) is None
    assert split_list("ISO-9001, IEC-62368") == ("ISO-9001", "IEC-62368")
    assert split_list(None) == () and split_list("0") == ()
