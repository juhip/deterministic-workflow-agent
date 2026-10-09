"""Exception handling: checked recovery options for every late order, with an optional model
recommendation. Code builds and checks the options; the model may only pick one and explain."""
import copy
import json

from fake_model import FakeModel
from procurement.db import load_state
from procurement.llm import LLMConfig, OpenAICompatibleClient
from procurement.pipeline import run
from procurement.rules import Rulebook

RULES = Rulebook.load()


def options_alert(res, order_id):
    return next((a.text for a in res.alerts if a.text.startswith(f"Recovery options for {order_id}")), None)


def s5(scenario_copy, rules=RULES, model=None):
    return run(load_state(scenario_copy("scenario_05_competing_demand.sqlite")), rules, model)


def test_s05_options_are_checked_and_priced(scenario_copy):
    text = options_alert(s5(scenario_copy), "WO-7102")
    assert "5 day(s) late, $69,480 of revenue at risk" in text
    # waiving the magnet memo: re-run says on time, costs more, and breaks the split
    assert "Waive MEMO-2026-018 (magnet supplier split, 60% cap) for this order: the order is on time; parts cost $471.50 more" in text
    assert "Ironbridge Magnet Co. would hold 100%" in text
    # extending the expired air-freight memo keeps the split AND the date
    assert "Extend MEMO-2026-044 (air-freight authorization) for this order: the order is on time" in text
    assert "Build 4 of 22 units by 2026-12-04 ($15,440 of revenue on time)" in text
    assert "Move the start to 2026-12-09 (5 day(s) later)" in text
    assert "Ask Halong Magnetics to deliver Samarium-Cobalt Magnet Segment by 2026-12-04 instead of 2026-12-09" in text
    assert text.endswith("Decision: the Sourcing Manager.")


def test_options_that_do_not_help_are_dropped(scenario_copy):
    """Scenario 1: no allowed magnet supplier can make 2026-10-16, so waiving the memo would not help."""
    res = run(load_state(scenario_copy("scenario_01_baseline.sqlite")), RULES)
    text = options_alert(res, "WO-7101")
    assert text and "Waive" not in text
    assert "Ask Halong" not in text            # Halong's order feeds a later production order, not this one


def test_hard_rules_are_never_waived(scenario_copy):
    """Scenario 3: capacitor packs need IEC-62368 and only Larchmont qualifies; boards are under the freeze. Never waived."""
    res = run(load_state(scenario_copy("scenario_03_tight_timeline.sqlite")), RULES)
    text = options_alert(res, "WO-7105")
    assert text and "Waive HF-PUR-100 §2" not in text
    assert "Ask Larchmont Electronics to deliver Circuit Board Assembly (4-layer), DC Link Capacitor Pack by 2026-10-13" in text


def test_no_late_orders_no_options(scenario_copy):
    res = run(load_state(scenario_copy("scenario_06_simple.sqlite")), RULES)
    assert not any(a.text.startswith("Recovery options") for a in res.alerts)


def test_options_never_change_the_plan(scenario_copy):
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["recovery_options"] = False
    off, on = s5(scenario_copy, Rulebook(raw)), s5(scenario_copy)
    key = lambda r: [(p.component_id, p.supplier_id, p.quantity, p.expected_delivery_date, p.rationale) for p in r.pos]
    assert key(on) == key(off)
    assert options_alert(off, "WO-7102") is None


# ---- the optional model recommendation ------------------------------------------------------
def advisor(pick):
    """Fake model: pick(exception) -> {"option":..., "reason":...}; other AI steps find nothing."""
    def reply(body):
        user = body["messages"][1]["content"]
        if user.startswith("EXCEPTIONS\n"):
            items = json.loads(user.split("EXCEPTIONS\n", 1)[1])
            return json.dumps({"recommendations": [{"id": e["id"], **pick(e)} for e in items]})
        if user.startswith("MESSAGES\n"):
            return json.dumps({"messages": []})
        return json.dumps({"parts": [], "risks": []})
    return FakeModel(reply)


def with_model(fake, scenario_copy):
    try:
        return s5(scenario_copy, model=OpenAICompatibleClient(LLMConfig(fake.url, "fake-open-model", timeout_s=5)))
    finally:
        fake.close()


def test_valid_recommendation_is_shown(scenario_copy):
    reason = "Extending the air-freight memo keeps the magnet split and makes the date; freight cost is the open question."
    res = with_model(advisor(lambda e: {"option": "B", "reason": reason}), scenario_copy)
    text = options_alert(res, "WO-7102")
    assert text.endswith(f"Recommended by fake-open-model: option B. {reason}")
    assert any("recommended an option for 1 of 1" in a.text for a in res.alerts if a.severity == "INFO")


def test_unknown_option_or_invented_fact_is_rejected(scenario_copy):
    res = with_model(advisor(lambda e: {"option": "Z", "reason": "Do something else."}), scenario_copy)
    assert "Recommended by" not in options_alert(res, "WO-7102")
    res = with_model(advisor(lambda e: {"option": "A", "reason": "Waiving saves $9,999 and Ostrander Shipyards pays it."}), scenario_copy)
    assert "Recommended by" not in options_alert(res, "WO-7102")
    assert any("rejected 1" in a.text and "9999" in a.text for a in res.alerts if a.severity == "INFO")


def test_model_down_still_lists_every_option(scenario_copy):
    plain = options_alert(s5(scenario_copy), "WO-7102")
    res = s5(scenario_copy, model=OpenAICompatibleClient(LLMConfig("http://127.0.0.1:9/v1", "x", timeout_s=2)))
    assert options_alert(res, "WO-7102") == plain
    assert any("option advisor skipped" in a.text for a in res.alerts)
