"""AI rule drafter: the model proposes, code checks, a person applies."""
import copy
import json
from pathlib import Path

import pytest

import propose_rules
from conftest import PROJECT, SCENARIO_DIR
from fake_model import FakeModel
from procurement.db import load_state
from procurement.rule_drafter import check, quote_in_memo, read_memo, unknown_memo_files
from procurement.rules import Rulebook

RULES = Rulebook.load()
MEMO = Path(__file__).parent / "data" / "memo_2026-11-02_domestic_premium.txt"

GOOD_RULE = {
    "id": "domestic-preference-memo-090", "type": "domestic_preference", "overrides": "domestic-preference",
    "source": {"document": "MEMO-2026-090", "section": "",
               "quote": "the domestic price premium threshold for critical parts is raised from 45% to 65%"},
    "effective_from": "2026-11-02", "effective_to": None,
    "scope": {"description": "all components", "selector": {}},
    "params": {"premium_threshold": 0.30, "premium_threshold_critical": 0.65}}


def reply(*changes, questions=()):
    return {"document": {"id": "MEMO-2026-090", "date": "2026-11-02"}, "summary": "Raises the critical threshold to 65%.",
            "changes": list(changes), "questions": list(questions)}


def test_good_proposal_passes_every_check():
    prop = check(reply({"action": "add", "rule": GOOD_RULE, "reason": "memo supersedes section 3"},
                       questions=["Add lead-time guarantees to contracts?"]), read_memo(MEMO), RULES)
    assert len(prop.accepted) == 1 and not prop.rejected
    new = Rulebook(prop.new_rulebook)
    from datetime import date
    active = new.active("domestic_preference", date(2026, 11, 9))
    assert [r.id for r in active] == ["domestic-preference-memo-090"]          # the memo replaces the base rule
    assert [r.id for r in new.active("domestic_preference", date(2026, 10, 5))] == ["domestic-preference"]
    assert "MEMO-2026-090" in {d["id"] for d in prop.new_rulebook["documents"]}
    assert prop.questions == ["Add lead-time guarantees to contracts?"]


def test_invented_quote_is_rejected():
    bad = copy.deepcopy(GOOD_RULE)
    bad["source"]["quote"] = "the threshold is raised to 90% for all parts"
    prop = check(reply({"action": "add", "rule": bad}), read_memo(MEMO), RULES)
    assert not prop.accepted and "quote does not appear" in prop.rejected[0][1]


@pytest.mark.parametrize("mutate,expected", [
    (lambda r: r.update(type="supplier_blacklist"), "not one the agent understands"),
    (lambda r: r.update(id="domestic-preference"), "already exists"),
    (lambda r: r["source"].update(document="MEMO-2026-018"), "must cite the memo"),
    (lambda r: r.update(overrides="no-such-rule"), "overrides unknown rule"),
    (lambda r: r["scope"].update(selector={"made_up_key": 1}), "would not load"),
])
def test_bad_changes_are_rejected(mutate, expected):
    bad = copy.deepcopy(GOOD_RULE)
    mutate(bad)
    prop = check(reply({"action": "add", "rule": bad}), read_memo(MEMO), RULES)
    assert not prop.accepted and expected in prop.rejected[0][1]
    assert prop.new_rulebook is None


def test_wrong_memo_id_rejects_everything():
    r = reply({"action": "add", "rule": GOOD_RULE})
    r["document"]["id"] = "MEMO-2099-001"
    prop = check(r, read_memo(MEMO), RULES)
    assert not prop.accepted and "does not appear in the memo" in prop.rejected[0][1]


def test_quote_matching_tolerates_spacing_and_ellipsis():
    memo = "Effective   immediately, the limit\nis reduced from 80% to 55% for any single supplier."
    assert quote_in_memo("the limit is reduced ... for any single supplier", memo)
    assert not quote_in_memo("the limit is removed", memo)


def test_cli_end_to_end_with_impact_and_apply(tmp_path, capsys, scenario_copy):
    rules_copy = tmp_path / "rules.json"
    rules_copy.write_text(json.dumps(RULES.raw))
    fake = FakeModel(json.dumps(reply({"action": "add", "rule": GOOD_RULE, "reason": "memo"})))
    try:
        code = propose_rules.main(["--memo", str(MEMO), "--rules", str(rules_copy), "--out-dir", str(tmp_path),
                                   "--llm-base-url", fake.url, "--llm-model", "fake-open-model",
                                   "--scenario", str(scenario_copy("scenario_05_competing_demand.sqlite"))])
    finally:
        fake.close()
    out = capsys.readouterr().out
    assert code == 0 and "+ ADD 'domestic-preference-memo-090'" in out
    # the flow sensor's 62% premium no longer exceeds the new 65% threshold: it moves to the domestic supplier
    # (the salinity sensor's 68% premium still does, so it stays international)
    assert "PT-113 from VEN-303: 40 -> 0" in out and "PT-113 from VEN-312: 0 -> 32" in out
    assert "PT-115" not in out
    assert json.loads(rules_copy.read_text()) == RULES.raw                 # nothing applied yet
    proposal = tmp_path / "MEMO-2026-090.json"
    assert propose_rules.main(["--apply", str(proposal), "--rules", str(rules_copy)]) == 0
    applied = Rulebook.load(rules_copy)
    assert applied.raw["status"] == "human-reviewed" and any(r.id == "domestic-preference-memo-090" for r in applied.rules)


def test_cli_reports_model_failure(tmp_path, capsys):
    fake = FakeModel("I think the memo is about magnets.")
    try:
        code = propose_rules.main(["--memo", str(MEMO), "--out-dir", str(tmp_path),
                                   "--llm-base-url", fake.url, "--llm-model", "fake-open-model"])
    finally:
        fake.close()
    assert code == 1 and "could not draft a proposal" in capsys.readouterr().out


def test_agent_flags_a_memo_the_rulebook_does_not_know(tmp_path):
    for f in [*(PROJECT / "data" / "policies").glob("*.pdf"), *(PROJECT / "data" / "memos").glob("*.pdf")]:
        (tmp_path / f.name).write_bytes(f.read_bytes())
    assert unknown_memo_files(tmp_path, RULES) == []
    (tmp_path / "memo_2026-11-02_domestic_premium.txt").write_text(MEMO.read_text())
    assert unknown_memo_files(tmp_path, RULES) == ["memo_2026-11-02_domestic_premium.txt"]
