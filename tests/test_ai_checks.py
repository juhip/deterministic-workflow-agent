"""AI supplier-note check inside the run (part classification is tested in test_part_labels.py).
The note check can only add caution (hold an order, raise an alert); code checks every answer."""
import json
import sqlite3

import pytest

import agent
from fake_model import FakeModel
from procurement.db import load_state
from procurement.llm import LLMConfig, OpenAICompatibleClient
from procurement.pipeline import run
from procurement.rules import Rulebook

RULES = Rulebook.load()


def is_labeller(body):
    return body["messages"][1]["content"].startswith("POLICY\n")       # the part classifier


def orders_in(body):
    return json.loads(body["messages"][1]["content"].split("ORDERS\n", 1)[1])


def model(parts=(), risks=lambda orders: []):
    """A fake model: `parts` answers the labeller; `risks(orders)` answers the supplier-notes check."""
    def reply(body):
        if "MESSAGES\n" in body["messages"][1]["content"]:
            return json.dumps({"messages": []})                    # message writer: keep every template
        if "EXCEPTIONS\n" in body["messages"][1]["content"]:
            return json.dumps({"recommendations": []})             # option advisor: no recommendation
        if is_labeller(body):
            return json.dumps({"parts": list(parts)})
        return json.dumps({"risks": risks(orders_in(body))})
    return FakeModel(reply)


def client(fake):
    return OpenAICompatibleClient(LLMConfig(fake.url, "fake-open-model", timeout_s=5))


def by_part(res, comp_id):
    return [(r, p) for r, p in zip(res.releases, res.pos) if p.component_id == comp_id]


def texts(res, severity=None):
    return [a.text for a in res.alerts if severity is None or a.severity == severity]


# ---- supplier notes -----------------------------------------------------------------------
def risk_for(supplier, quote, text="Capacity may not cover this order."):
    return lambda orders: [{"order": o["label"], "quote": quote, "risk": text} for o in orders if o["supplier"] == supplier]


def test_capacity_note_is_raised_against_the_magnet_order(scenario_copy):
    state = load_state(scenario_copy("scenario_01_baseline.sqlite"))
    fake = model(risks=risk_for("Ironbridge Magnet Co.", "small batch capacity", "65 SmCo magnets may exceed Ironbridge's capacity."))
    try:
        res = run(state, RULES, client(fake))
    finally:
        fake.close()
    ironbridge = [(r, p) for r, p in by_part(res, "PT-103") if p.supplier_id == "VEN-308"]
    assert any('supplier note "small batch capacity"' in x for x in ironbridge[0][0].reasons)
    assert any("Supplier note risk" in t and "small batch capacity" in t for t in texts(res, "WARNING"))


def test_note_risk_holds_an_otherwise_routine_order(scenario_copy):
    state = load_state(scenario_copy("scenario_01_baseline.sqlite"))
    assert not by_part(run(state, RULES), "PT-116")[0][0].needs_approval          # probe housings: routine without a model
    fake = model(risks=risk_for("Ridgeway Machine Works", "Dependable on machined and cast parts", "Housings may need checking."))
    try:
        res = run(state, RULES, client(fake))
    finally:
        fake.close()
    assert by_part(res, "PT-116")[0][0].needs_approval


def test_invented_quote_is_dropped(scenario_copy):
    state = load_state(scenario_copy("scenario_01_baseline.sqlite"))
    fake = model(risks=risk_for("Ridgeway Machine Works", "factory fire last month"))
    try:
        res = run(state, RULES, client(fake))
    finally:
        fake.close()
    assert not by_part(res, "PT-116")[0][0].needs_approval
    assert not any("Supplier note risk" in t for t in texts(res))


# ---- failure and safety --------------------------------------------------------------------
def test_unreachable_model_changes_nothing(scenario_copy):
    state = load_state(scenario_copy("scenario_05_competing_demand.sqlite"))
    plain = run(state, RULES)
    res = run(state, RULES, OpenAICompatibleClient(LLMConfig("http://127.0.0.1:9/v1", "x", timeout_s=2)))
    assert [(p.component_id, p.supplier_id, p.quantity) for p in res.pos] == \
           [(p.component_id, p.supplier_id, p.quantity) for p in plain.pos]
    assert [r.needs_approval for r in res.releases] == [r.needs_approval for r in plain.releases]
    assert any("skipped" in t for t in texts(res, "INFO"))


def test_supplier_notes_never_change_orders(scenario_copy):
    state = load_state(scenario_copy("scenario_04_low_inventory.sqlite"))
    plain = run(state, RULES)
    fake = model(risks=lambda orders: [{"order": o["label"], "quote": (o["supplier_notes"] or "x")[:12], "risk": "r"}
                                       for o in orders])
    try:
        res = run(state, RULES, client(fake))
    finally:
        fake.close()
    key = lambda r: [(p.component_id, p.supplier_id, p.quantity, p.unit_price, p.expected_delivery_date) for p in r.pos]
    assert key(res) == key(plain)                                   # same orders, quantities, prices and dates
    assert sum(r.needs_approval for r in res.releases) >= sum(r.needs_approval for r in plain.releases)


def test_cli_runs_ai_checks_and_writes_alerts(scenario_copy):
    path = scenario_copy("scenario_01_baseline.sqlite")
    fake = model(risks=risk_for("Ironbridge Magnet Co.", "small batch capacity"))
    try:
        agent.main(["--scenario", str(path), "--quiet", "--llm-base-url", fake.url, "--llm-model", "fake-open-model"])
    finally:
        fake.close()
    con = sqlite3.connect(path)
    alerts = [r[0] for r in con.execute("SELECT description FROM alerts ORDER BY alert_id")]
    con.close()
    assert any("Supplier note risk" in a for a in alerts) and alerts[-1].startswith("[INFO] Run summary")
