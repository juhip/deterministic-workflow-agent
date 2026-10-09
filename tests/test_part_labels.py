"""The model is the primary reader of parts; the rulebook's keyword rules are the safety floor.

Held-out scenarios may name parts differently. These tests rename three parts so the keyword
rules cannot recognise them, then check that the model's (verified) labels drive the real
decisions: the magnet split, the circuit-board freeze and the IEC-62368 certificate."""
import json
import sqlite3

import pytest

from fake_model import FakeModel
from procurement.db import load_state
from procurement.llm import LLMConfig, OpenAICompatibleClient
from procurement.pipeline import run
from procurement.rules import Rulebook

RULES = Rulebook.load()
RENAMED = {   # part -> (new name, new description): none of the rulebook's keywords match these
    "PT-103": ("Rotor Magnet, Rare-Earth (Grade SmCo)", "Permanent magnet set for pump rotors"),
    "PT-105": ("Populated Circuit Card, 4L", "Main controller card"),
    "PT-117": ("Bulk Capacitor Array", "Energy storage for the power stage"),
}
TRUTH = {     # what a correct model says about them
    "PT-103": {"critical_category": "samarium-cobalt magnet", "rules": ["concentration-magnets"]},
    "PT-105": {"critical_category": "circuit board", "rules": ["cert-pcb", "pcb-supplier-freeze", "pcb-coc"]},
    "PT-117": {"critical_category": None, "rules": ["cert-power-supply"]},
}


def renamed_s5(scenario_copy):
    path = scenario_copy("scenario_05_competing_demand.sqlite")
    con = sqlite3.connect(path)
    for cid, (name, desc) in RENAMED.items():
        con.execute("UPDATE components SET name = ?, description = ? WHERE component_id = ?", (name, desc, cid))
    con.commit(); con.close()
    return load_state(path)


def classifier(answer):
    """Fake model. answer(part) -> {"critical_category", "rules"} or None; other AI steps find nothing."""
    def reply(body):
        user = body["messages"][1]["content"]
        if user.startswith("POLICY\n"):
            parts = json.loads(user.split("PARTS\n", 1)[1])
            out = []
            for p in parts:
                a = answer(p)
                out.append({"component_id": p["component_id"], "reason": "test",
                            **(a or {"critical_category": None, "rules": []})})
            return json.dumps({"parts": out})
        if user.startswith("EXCEPTIONS\n"):
            return json.dumps({"recommendations": []})
        return json.dumps({"risks": [], "messages": []})
    return FakeModel(reply)


def run_with(fake, state, cache_dir=None):
    try:
        return run(state, RULES, OpenAICompatibleClient(LLMConfig(fake.url, "fake-open-model", timeout_s=5)),
                   cache_dir=cache_dir)
    finally:
        fake.close()


def magnet_shares(res):
    vol = {}
    for p in res.pos:
        if p.component_id == "PT-103":
            vol[p.supplier_id] = vol.get(p.supplier_id, 0) + p.quantity
    return {s: q / sum(vol.values()) for s, q in vol.items()}


def test_without_a_model_renamed_parts_escape_the_rules(scenario_copy):
    """The gap this design closes: keyword rules cannot see a renamed part."""
    res = run(renamed_s5(scenario_copy), RULES)
    assert "concentration-magnets" not in res.tags["PT-103"].rule_ids
    assert "pcb-supplier-freeze" not in res.tags["PT-105"].rule_ids
    assert "IEC-62368" not in res.tags["PT-117"].required_certs


def test_model_labels_drive_the_real_decisions(scenario_copy):
    res = run_with(classifier(lambda p: TRUTH.get(p["component_id"])), renamed_s5(scenario_copy))
    shares = magnet_shares(res)
    assert len(shares) == 2 and max(shares.values()) <= 0.6 + 1e-9          # MEMO-2026-018 split applied
    assert res.tags["PT-103"].critical and "read by the model" in res.tags["PT-103"].critical_reason
    assert {p.supplier_id for p in res.pos if p.component_id == "PT-105"} == {"VEN-301"}   # freeze applied
    assert "IEC-62368" in res.tags["PT-117"].required_certs                   # power-supply certificate required
    held = [r for r, p in zip(res.releases, res.pos) if p.component_id in RENAMED]
    assert held and all(any("classified by the model" in x for x in r.reasons) for r in held)
    assert any(a.severity == "WARNING" and "keywords missed" in a.text for a in res.alerts)
    # recovery options re-run the plan with the same labels, so waiving the magnet memo is on the list
    assert any("Waive MEMO-2026-018" in a.text for a in res.alerts if a.text.startswith("Recovery options"))


def test_made_up_labels_and_rule_ids_are_ignored(scenario_copy):
    res = run_with(classifier(lambda p: {"critical_category": "rocket part", "rules": ["no-such-rule"]}),
                   renamed_s5(scenario_copy))
    assert not res.tags["PT-103"].critical
    assert res.tags["PT-103"].rule_ids == []


def test_model_can_add_rules_but_never_remove_them(scenario_copy):
    """Original names, and a model that says nothing applies to anything: the keyword floor holds."""
    state = load_state(scenario_copy("scenario_05_competing_demand.sqlite"))
    plain = run(state, RULES)
    res = run_with(classifier(lambda p: None), state)
    key = lambda r: [(p.component_id, p.supplier_id, p.quantity) for p in r.pos]
    assert key(res) == key(plain)
    assert any("stricter reading was kept" in a.text for a in res.alerts)


def test_labels_are_cached_so_reruns_see_the_same_answer(scenario_copy, tmp_path):
    calls = []
    def answer(p):
        calls.append(p["component_id"])
        return TRUTH.get(p["component_id"])
    state = renamed_s5(scenario_copy)
    first = run_with(classifier(answer), state, cache_dir=tmp_path)
    asked = len(calls)
    second = run_with(classifier(answer), state, cache_dir=tmp_path)
    assert asked > 0 and len(calls) == asked                                  # second run asked nothing
    assert [(p.component_id, p.supplier_id, p.quantity) for p in first.pos] == \
           [(p.component_id, p.supplier_id, p.quantity) for p in second.pos]


def test_model_down_falls_back_to_the_keyword_rules(scenario_copy):
    state = load_state(scenario_copy("scenario_05_competing_demand.sqlite"))
    plain = run(state, RULES)
    res = run(state, RULES, OpenAICompatibleClient(LLMConfig("http://127.0.0.1:9/v1", "x", timeout_s=2)))
    assert [(p.component_id, p.supplier_id) for p in res.pos] == [(p.component_id, p.supplier_id) for p in plain.pos]
    assert any("classification skipped" in a.text for a in res.alerts)
