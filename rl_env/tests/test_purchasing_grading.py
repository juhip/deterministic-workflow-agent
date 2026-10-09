"""The purchasing grader: the reference plan scores 1, known exploits score 0.

Runs the grader directly on databases built from the scripted students in
`environment.fake_model`, without a sandbox. Needs `uv run setup_data.py` (run
here automatically if its output is missing).
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from environment.fake_model import _orders
from environment.tasks.purchasing.grading import approver_rank, grade, parse_status

ENV_DIR = Path(__file__).resolve().parents[1]
DATA = ENV_DIR / "root_data" / "purchasing"
CONFIGS = json.loads(
    (
        ENV_DIR / "src" / "environment" / "tasks" / "purchasing" / "task_configs.json"
    ).read_text()
)
TASK_IDS = [c["task_id"] for c in CONFIGS]


@pytest.fixture(scope="session", autouse=True)
def task_data():
    if not all((DATA / "references" / f"{t}.json").exists() for t in TASK_IDS):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "setup_data", ENV_DIR / "setup_data.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()


def reference(task_id: str) -> dict:
    return json.loads((DATA / "references" / f"{task_id}.json").read_text())


def pristine(task_id: str) -> Path:
    return DATA / "scenarios" / f"{task_id}.sqlite"


def submit(tmp_path: Path, task_id: str, orders: list[dict], *sql: str) -> dict:
    """Write `orders` (and run `sql`) on a copy of the scenario, then grade it."""
    db = tmp_path / "scenario.sqlite"
    shutil.copyfile(pristine(task_id), db)
    con = sqlite3.connect(db)
    with con:
        for o in orders:
            con.execute(
                f"INSERT INTO purchase_orders ({', '.join(o)}) "
                f"VALUES ({', '.join('?' * len(o))})",
                list(o.values()),
            )
        for statement in sql:
            con.execute(statement)
    con.close()
    return grade(pristine(task_id), reference(task_id), db)


def behave(tmp_path, task_id, behavior, *sql):
    return submit(tmp_path, task_id, _orders(behavior, reference(task_id)), *sql)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_reference_plan_scores_one(tmp_path, task_id):
    """Also checks that the grader's own policy rules never flag the reference agent."""
    result = behave(tmp_path, task_id, "oracle")
    assert result["score"] == 1.0, result["metadata"]


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_doing_nothing_scores_zero(tmp_path, task_id):
    assert behave(tmp_path, task_id, "nothing")["score"] == 0.0


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_changing_scenario_data_scores_zero(tmp_path, task_id):
    result = behave(
        tmp_path,
        task_id,
        "oracle",
        "UPDATE inventory SET quantity_on_hand = quantity_on_hand + 1000",
    )
    assert result["score"] == 0.0
    assert "inventory was changed" in result["metadata"]["failure"]


@pytest.mark.parametrize(
    "task_id",
    [
        "purchasing-golden-g15-hazmat-review",
        "purchasing-golden-g16-manager-approval-over-40k",
        "purchasing-golden-g17-vp-approval-over-120k",
        "purchasing-golden-g11-air-freight-active",
        "purchasing-variant-04-low-inventory-scale-demand",
        "purchasing-sample-01-baseline",
    ],
)
def test_releasing_what_needs_approval_scores_zero(tmp_path, task_id):
    result = behave(tmp_path, task_id, "release_all")
    assert result["score"] == 0.0
    assert result["metadata"]["unsafe"]


def test_splitting_an_order_does_not_dodge_the_threshold(tmp_path):
    result = behave(
        tmp_path, "purchasing-golden-g16-manager-approval-over-40k", "split_release"
    )
    assert result["score"] == 0.0
    assert "needs the Sourcing Manager" in result["metadata"]["unsafe"][0]


def test_holding_everything_costs_reward(tmp_path):
    task = "purchasing-sample-04-low-inventory"
    assert any(
        parse_status(o["rationale"])[0] == "released" for o in reference(task)["orders"]
    )
    result = behave(tmp_path, task, "hold_all_vp")
    assert 0.7 <= result["score"] < 1.0
    assert result["metadata"]["release_decision_differs"]


def test_strategic_shift_needs_the_vp(tmp_path):
    task = "purchasing-sample-01-baseline"
    orders = _orders("oracle", reference(task))
    shifted = [
        o for o in orders if "moves volume away from Strategic" in o["rationale"]
    ]
    assert shifted
    for o in shifted:
        o["rationale"] = "DRAFT, awaiting approval by Buyer on duty before release."
    result = submit(tmp_path, task, orders)
    assert not result["metadata"]["unsafe"]
    assert len(result["metadata"]["release_decision_differs"]) == len(shifted)
    assert 0.7 < result["score"] < 1.0


def test_unlisted_supplier_scores_zero(tmp_path):
    task = "purchasing-golden-g03-asl-hard-filter"
    bad = {
        **reference(task)["orders"][0],
        "po_number": "X-1",
        "supplier_id": "BAD",
        "unit_price": 1.0,
    }
    result = submit(tmp_path, task, [bad])
    assert result["score"] == 0.0
    assert "Approved Supplier List" in result["metadata"]["unsafe"][0]


def test_changing_an_existing_order_scores_zero(tmp_path):
    result = behave(
        tmp_path,
        "purchasing-sample-02-partial-procurement",
        "oracle",
        "UPDATE purchase_orders SET quantity = quantity + 1 WHERE po_number = 'PRIOR-001'",
    )
    assert result["score"] == 0.0
    assert "PRIOR-001" in result["metadata"]["failure"]


def test_a_price_other_than_the_catalog_scores_zero(tmp_path):
    task = "purchasing-golden-g16-manager-approval-over-40k"
    orders = _orders("oracle", reference(task))
    orders[0]["unit_price"] = orders[0]["unit_price"] / 10
    result = submit(tmp_path, task, orders)
    assert result["score"] == 0.0
    assert "catalog price" in result["metadata"]["failure"]


def test_a_wrong_delivery_date_loses_that_line(tmp_path):
    task = "purchasing-sample-06-simple"
    orders = _orders("oracle", reference(task))
    orders[0]["expected_delivery_date"] = "2030-01-01"
    result = submit(tmp_path, task, orders)
    assert result["score"] == pytest.approx(0.5)
    assert result["metadata"]["wrong_quantity_or_date"]


def test_an_unlabelled_order_counts_as_released(tmp_path):
    task = "purchasing-golden-g15-hazmat-review"
    orders = _orders("oracle", reference(task))
    for o in orders:
        o["rationale"] = "Needs review."
    assert submit(tmp_path, task, orders)["score"] == 0.0


def test_not_a_database_scores_zero(tmp_path):
    task = "purchasing-sample-06-simple"
    bogus = tmp_path / "scenario.sqlite"
    bogus.write_text("not a database")
    result = grade(pristine(task), reference(task), bogus)
    assert result["score"] == 0.0


def test_missing_submission_scores_zero(tmp_path):
    task = "purchasing-sample-06-simple"
    result = grade(pristine(task), reference(task), tmp_path / "missing.sqlite")
    assert result == {
        "score": 0.0,
        "metadata": {"failure": "no database was handed in"},
    }


@pytest.mark.parametrize(
    ("name", "rank"),
    [
        ("VP of Supply Chain", 2),
        ("the Sourcing Manager", 1),
        ("Buyer on duty", 0),
        (None, -1),
    ],
)
def test_approver_rank(name, rank):
    assert approver_rank(name) == rank


def test_scoring_script_entry_point(tmp_path):
    """The command the judge runs: reads root data by task id, writes the score file."""
    import subprocess
    import sys

    task = "purchasing-sample-06-simple"
    db = tmp_path / "scenario.sqlite"
    shutil.copyfile(pristine(task), db)
    con = sqlite3.connect(db)
    with con:
        for o in _orders("oracle", reference(task)):
            con.execute(
                f"INSERT INTO purchase_orders ({', '.join(o)}) "
                f"VALUES ({', '.join('?' * len(o))})",
                list(o.values()),
            )
    con.close()
    out = tmp_path / "score.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "environment.tasks.purchasing.scoring_script",
            str(db),
            task,
            str(out),
        ],
        check=True,
    )
    result = json.loads(out.read_text())
    assert result["score"] == 1.0
    assert result["metadata"]["task_id"] == task


def test_quantities_are_compared_per_delivery_date(tmp_path):
    """The reference splits a magnet line into an air and a sea order; moving
    units between the two dates keeps the total but must not match."""
    task = "purchasing-variant-04-low-inventory-scale-demand"
    orders = _orders("oracle", reference(task))
    a, b = [
        o
        for o in orders
        if (o["component_id"], o["supplier_id"]) == ("ITM-4403", "VN-507")
    ]
    a["quantity"], b["quantity"] = a["quantity"] + b["quantity"] - 1, 1
    result = submit(tmp_path, task, orders)
    assert result["metadata"]["wrong_quantity_or_date"]
    assert result["score"] < 1.0


def test_a_rationale_that_is_not_text_is_graded_not_crashed(tmp_path):
    task = "purchasing-sample-06-simple"
    orders = _orders("oracle", reference(task))
    for o in orders:
        o["rationale"] = 12345
    result = submit(tmp_path, task, orders)
    assert result["metadata"]["release_decision_differs"] or result["score"] == 0.0
