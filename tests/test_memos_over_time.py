"""Memos update policy over time: the same scenario, run on different dates, must follow
whichever memos are in force on that date.

Memo windows (from the rulebook):
  MEMO-2026-018  magnet 60% cap + 25% secondary   from 2026-05-18, no end
  MEMO-2026-044  air freight for international     2026-08-03 to 2026-11-02
  MEMO-2026-051  circuit-board supplier freeze     from 2026-09-14, no end
"""
import sqlite3

import pytest

from procurement.db import load_state
from procurement.pipeline import run
from procurement.rules import Rulebook

RULES = Rulebook.load()


def run_on(scenario_copy, day: str):
    """Scenario 1 with its 'today' moved to `day` (production dates unchanged)."""
    path = scenario_copy("scenario_01_baseline.sqlite")
    con = sqlite3.connect(path)
    con.execute('UPDATE scenario_config SET "current_date" = ?', (day,))
    con.commit()
    con.close()
    return run(load_state(path), RULES)


def eligible(res, comp_id):
    return {o.supplier.supplier_id for o in res.decisions[comp_id].options if o.eligible}


def has_air(res):
    return any(o.mode == "air" for d in res.decisions.values() for o in d.options)


def magnet_suppliers(res):
    return {p.supplier_id for p in res.pos if p.component_id == "PT-103"}


def test_before_any_memo(scenario_copy):
    res = run_on(scenario_copy, "2026-03-02")
    assert res.decisions["PT-103"].concentration.id == "concentration-default"   # policy's 65%, not the memo's 60%
    assert magnet_suppliers(res) == {"VEN-308"}                                   # no forced split
    assert {"VEN-303", "VEN-310"} <= eligible(res, "PT-105")                     # no circuit-board freeze yet
    assert not has_air(res)                                                        # no air-freight authorization yet


def test_magnet_memo_starts_on_its_date(scenario_copy):
    assert run_on(scenario_copy, "2026-05-17").decisions["PT-103"].concentration.id == "concentration-default"
    res = run_on(scenario_copy, "2026-05-18")
    assert res.decisions["PT-103"].concentration.id == "concentration-magnets"
    assert magnet_suppliers(res) == {"VEN-307", "VEN-308"}                        # split enforced from day one


def test_air_freight_window_is_inclusive(scenario_copy):
    assert not has_air(run_on(scenario_copy, "2026-08-02"))
    assert has_air(run_on(scenario_copy, "2026-08-03"))
    assert has_air(run_on(scenario_copy, "2026-11-02"))
    assert not has_air(run_on(scenario_copy, "2026-11-03"))                       # memo has ended


def test_circuit_board_freeze_starts_on_its_date(scenario_copy):
    before = run_on(scenario_copy, "2026-09-13")
    assert {"VEN-303", "VEN-310"} <= eligible(before, "PT-105")
    after = run_on(scenario_copy, "2026-09-14")
    assert eligible(after, "PT-105") == {"VEN-301"}
    assert any("freeze" in " ".join(o.excluded_because) for o in after.decisions["PT-105"].options)


@pytest.mark.parametrize("day,air,freeze,magnet_memo", [
    ("2026-03-02", False, False, False),
    ("2026-06-01", False, False, True),
    ("2026-08-17", True, False, True),
    ("2026-10-05", True, True, True),
    ("2026-11-09", False, True, True),
])
def test_memo_timeline(scenario_copy, day, air, freeze, magnet_memo):
    """One table showing which memos are in force on each date."""
    res = run_on(scenario_copy, day)
    assert has_air(res) is air
    assert (eligible(res, "PT-105") == {"VEN-301"}) is freeze
    assert (res.decisions["PT-103"].concentration.id == "concentration-magnets") is magnet_memo
    assert res.pos and all(p.quantity > 0 for p in res.pos)       # it still plans orders on every date


def test_rationale_cites_the_memo_in_force(scenario_copy):
    res = run_on(scenario_copy, "2026-10-05")
    magnet_reasons = " ".join(p.rationale for p in res.pos if p.component_id == "PT-103")
    assert "MEMO-2026-018" in magnet_reasons
    early = run_on(scenario_copy, "2026-03-02")
    assert "MEMO-2026-018" not in " ".join(p.rationale for p in early.pos if p.component_id == "PT-103")
