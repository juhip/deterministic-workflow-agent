"""The optional AI message writer: the model may change how a message reads, never what it says."""
import copy
import json
import re
import sqlite3

import agent
from fake_model import FakeModel
from procurement.db import load_state
from procurement.llm import LLMConfig, OpenAICompatibleClient
from procurement.messages import check
from procurement.pipeline import run
from procurement.rules import Rulebook

RULES_RAW = copy.deepcopy(Rulebook.load().raw)
RULES_RAW["settings"]["rewrite_messages_with_model"] = True      # off by default; these tests switch it on
RULES = Rulebook(RULES_RAW)


def writer(edit):
    """A fake model: edit(text) -> rewritten text for every message it is sent."""
    def reply(body):
        user = body["messages"][1]["content"]
        if not user.startswith("MESSAGES\n"):
            return json.dumps({"parts": [], "risks": []})          # other AI checks: find nothing
        items = json.loads(user.split("MESSAGES\n", 1)[1])
        return json.dumps({"messages": [{"id": m["id"], "text": edit(m["text"])} for m in items]})
    return FakeModel(reply)


def run_with(fake, scenario_copy, name="scenario_05_competing_demand.sqlite", rules=RULES):
    client = OpenAICompatibleClient(LLMConfig(fake.url, "fake-open-model", timeout_s=5))
    try:
        return run(load_state(scenario_copy(name)), rules, client)
    finally:
        fake.close()


def by_start(res, prefix):
    return [a.text for a in res.alerts if a.text.startswith(prefix)]


def test_faithful_rewrite_is_used(scenario_copy):
    res = run_with(writer(lambda t: "Action for you. " + t), scenario_copy)
    late = [a for a in res.alerts if a.severity == "CRITICAL"]
    assert late and all(a.text.startswith("Action for you.") for a in late)
    assert all("every figure checked" in a.text for a in late)
    assert any("rewrote" in a.text and "of" in a.text for a in res.alerts if a.severity == "INFO")
    assert res.alerts[-1].text.startswith("Run summary")


def test_invented_number_is_rejected(scenario_copy):
    res = run_with(writer(lambda t: t + " About 999 extra units may be needed."), scenario_copy)
    assert not any("999" in a.text for a in res.alerts if a.severity in ("ACTION", "CRITICAL"))
    note = next(a.text for a in res.alerts if "message writer" in a.text)
    assert "kept the original" in note and "added 999" in note


def test_dropped_amount_or_changed_date_is_rejected(scenario_copy):
    drop_money = lambda t: re.sub(r"\$\d[\d,]*(?:\.\d+)?", "a sum", t, count=1)
    res = run_with(writer(drop_money), scenario_copy)
    assert not any("a sum" in a.text for a in res.alerts if a.severity in ("ACTION", "CRITICAL"))
    shift_date = lambda t: t.replace("2026-12-09", "2026-12-08")
    res = run_with(writer(shift_date), scenario_copy)
    assert not any("2026-12-08" in a.text for a in res.alerts if a.severity in ("ACTION", "CRITICAL"))
    assert any("kept the original" in a.text for a in res.alerts if a.severity == "INFO")


def test_rewrite_never_changes_orders(scenario_copy):
    plain = run(load_state(scenario_copy("scenario_05_competing_demand.sqlite")), RULES)
    res = run_with(writer(lambda t: "Short version. " + t), scenario_copy)
    key = lambda r: [(p.component_id, p.supplier_id, p.quantity, p.unit_price, p.rationale) for p in r.pos]
    assert key(res) == key(plain)                       # order rationales are the audit record, never rewritten
    assert [r.approver for r in res.releases] == [r.approver for r in plain.releases]


def test_setting_turns_it_off(scenario_copy):
    raw = copy.deepcopy(RULES.raw)
    raw["settings"]["rewrite_messages_with_model"] = False
    res = run_with(writer(lambda t: "Short version. " + t), scenario_copy, rules=Rulebook(raw))
    assert not any(a.text.startswith("Short version.") for a in res.alerts)


def test_unreachable_model_keeps_every_message(scenario_copy):
    state = load_state(scenario_copy("scenario_05_competing_demand.sqlite"))
    plain = run(state, RULES)
    res = run(state, RULES, OpenAICompatibleClient(LLMConfig("http://127.0.0.1:9/v1", "x", timeout_s=2)))
    texts = {a.text for a in plain.alerts if a.severity in ("ACTION", "CRITICAL")}
    assert texts <= {a.text for a in res.alerts}
    assert any("message writer skipped" in a.text for a in res.alerts)


def test_check_unit_cases():
    names = {"Ostrander Shipyards", "Halong Magnetics"}
    original = "Ostrander Shipyards is 5 day(s) late; $594.50 order from Halong Magnetics arrives 2026-12-09."
    assert check(original, "Halong Magnetics ($594.50) arrives 2026-12-09, so Ostrander Shipyards is 5 days late.", names) is None
    assert "dropped 5" in check(original, "Ostrander Shipyards is late; $594.50 from Halong Magnetics on 2026-12-09.", names)
    cited = original + " (MEMO-2026-018, HF-PUR-100 §6)"
    assert check(cited, original, names) is None                          # citations may be dropped
    assert "added" in check(original, cited, names)                        # but never invented
    assert "dropped" in check(original, "Ostrander Shipyards is 5 days late; $594.50 order arrives 2026-12-09.", names)
    assert "added" in check(original, original + " Also call Ironbridge Magnet Co.", names | {"Ironbridge Magnet Co."})


def test_cli_writes_rewritten_alerts(scenario_copy, tmp_path):
    path = scenario_copy("scenario_05_competing_demand.sqlite")
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(RULES_RAW))
    fake = writer(lambda t: "For your decision. " + t)
    try:
        agent.main(["--scenario", str(path), "--quiet", "--rules", str(rules_file),
                    "--llm-base-url", fake.url, "--llm-model", "fake-open-model"])
    finally:
        fake.close()
    con = sqlite3.connect(path)
    alerts = [r[0] for r in con.execute("SELECT description FROM alerts ORDER BY alert_id")]
    con.close()
    assert any(a.startswith("[CRITICAL] For your decision.") for a in alerts)
    assert alerts[-1].startswith("[INFO] Run summary")
