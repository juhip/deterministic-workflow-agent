"""The policy scorecard (evals/policy_scorecard.py) grades the agent clause by clause.
These tests prove two things: the agent passes it, and the scorecard really catches a failed check."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import policy_scorecard as ps                      # noqa: E402
from procurement.rules import Rulebook             # noqa: E402

RULES = Rulebook.load()


@pytest.fixture(autouse=True)
def fresh_checks():
    for c in ps.CHECKS.values():
        c.checked, c.failed = 0, []
    yield


def failed():
    return {k: c.failed for k, c in ps.CHECKS.items() if c.failed}


@pytest.mark.parametrize("src", ps.SCENARIOS, ids=lambda p: p.stem)
def test_agent_passes_every_policy_check(src):
    ps.grade(src, src.stem, RULES)
    assert failed() == {}


def test_scorecard_catches_a_planted_bad_order(monkeypatch):
    """After the agent runs, plant an order from the supplier removed from the approved list,
    with a made-up delivery date. The scorecard must flag both clauses."""
    real = ps.run_agent

    def run_then_tamper(path):
        real(path)
        con = sqlite3.connect(path)
        if not con.execute("SELECT 1 FROM purchase_orders WHERE po_number = 'BAD-1'").fetchone():
            day = con.execute('SELECT "current_date" FROM scenario_config').fetchone()[0]
            con.execute("INSERT INTO purchase_orders VALUES ('BAD-1', 'PT-105', 'VEN-313', 100, 5.4, ?, ?, "
                        "'RELEASED automatically: cheap boards')", (day, day))
            con.commit()
        con.close()

    monkeypatch.setattr(ps, "run_agent", run_then_tamper)
    ps.grade(ps.DATA / "scenario_06_simple.sqlite", "tampered", RULES)
    f = failed()
    assert any("BAD-1" in x for x in f["asl"])           # Policy §2
    assert any("BAD-1" in x for x in f["leadtime"])      # Policy §10
    assert any("BAD-1" in x for x in f["pcb"])           # MEMO-2026-051


def test_holds_are_split_into_policy_required_and_caution():
    """The Useful tally: a hold counts as policy-required only if the policy asks for a person."""
    import policy_scorecard as ps
    saved = {k: (v if not isinstance(v, dict) else ps.defaultdict(int, v)) for k, v in ps.HOLDS.items()}
    ps.HOLDS.update(orders=0, released=0, policy=0, caution=0, reasons=ps.defaultdict(int))
    try:
        ps.tally_holds([
            {"why": "RELEASED automatically: routine order. Buy 5 units."},
            {"why": "DRAFT, awaiting approval by X before release to the supplier (because: hazardous material "
                    "(procurement review required); critical part (IGBT power module (HF-PUR-100 §4))). Buy 5 units."},
            {"why": "DRAFT, awaiting approval by X before release to the supplier (because: critical part "
                    "(IGBT power module (HF-PUR-100 §4)); only one supplier is allowed for this part). Buy 5 units."},
        ])
        assert (ps.HOLDS["released"], ps.HOLDS["policy"], ps.HOLDS["caution"]) == (1, 1, 1)
        assert "caution rule" in ps.holds_report() and "Policy §6" in ps.holds_report()
    finally:
        ps.HOLDS.update(saved)
