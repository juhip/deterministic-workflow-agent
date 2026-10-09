# This script gets executed on the build machine that builds your environment,
# but before the environment is created. That means you cannot depend on system
# packages that are not installed on the build machine, so keep it simple.
# Add any dependencies required for data setup in the section below.

# /// script
# requires-python = "==3.12.*"
# dependencies = []
# ///
"""Build the purchasing tasks from the agent repository this environment lives in.

For every scenario (the six samples, the 17 golden cases, and the generated variants) it

  * saves the scenario as the student will get it under root_data/purchasing/scenarios/
    (scenario_description blanked: in the golden cases it names the rule being tested)
  * runs the reference agent (../agent.py, no model) on a copy and saves the purchase orders
    and alerts it wrote under root_data/purchasing/references/
  * lists the task in src/environment/tasks/purchasing/task_configs.json

The policy and memo PDFs go to student_data/documents/. The agent's code, its rulebook and the
golden answers never enter the image: the grader only needs the saved references.
"""

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ENV_DIR = Path(__file__).resolve().parent
REPO = ENV_DIR.parent
OUT = ENV_DIR / "root_data" / "purchasing"
DOCS = ENV_DIR / "student_data" / "documents"
TASK_CONFIGS = (
    ENV_DIR / "src" / "environment" / "tasks" / "purchasing" / "task_configs.json"
)


def _scenarios(tmp: Path) -> list[dict]:
    """Every task's id, source scenario, kind and what it tests, in a stable order."""
    sys.path.insert(0, str(REPO / "evals"))
    import gen_scenarios  # the repo's own scenario generator

    found = []
    for path in sorted((REPO / "data" / "scenarios").glob("scenario_*.sqlite")):
        found.append(
            {
                "task_id": "purchasing-sample-"
                + path.stem.removeprefix("scenario_").replace("_", "-"),
                "source": path,
                "kind": "sample",
                "tests": "one of the six provided sample scenarios",
            }
        )
    golden = json.loads((REPO / "evals" / "golden" / "golden_cases.json").read_text())
    for case in golden:
        found.append(
            {
                "task_id": "purchasing-golden-" + case["id"].lower().replace("_", "-"),
                "source": REPO / "evals" / "golden" / "cases" / f"{case['id']}.sqlite",
                "kind": "golden",
                "tests": case["title"],
            }
        )
    made = gen_scenarios.make_all(tmp / "variants") + gen_scenarios.make_structural(
        tmp / "structural"
    )
    for path, why in made:
        found.append(
            {
                "task_id": "purchasing-variant-"
                + path.stem.removeprefix("scenario_").replace("_", "-"),
                "source": path,
                "kind": "variant",
                "tests": why,
            }
        )
    return found


def _rows(db: Path, sql: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def _prepare(source: Path, dest: Path) -> str:
    """Copy `source` to `dest` as the student gets it; returns the scenario's current date."""
    shutil.copyfile(source, dest)
    sys.path.insert(0, str(REPO / "evals"))
    from datadir import as_provided

    as_provided(dest)  # drop anything an earlier agent run left behind
    con = sqlite3.connect(dest)
    try:
        with con:
            con.execute("UPDATE scenario_config SET scenario_description = ''")
        con.execute("VACUUM")
        return con.execute('SELECT "current_date" FROM scenario_config').fetchone()[0]
    finally:
        con.close()


def _reference(pristine: Path, work: Path) -> dict:
    """Run the reference agent on a copy of `pristine`; return what it added."""
    shutil.copyfile(pristine, work)
    done = subprocess.run(
        [
            sys.executable,
            str(REPO / "agent.py"),
            "--scenario",
            str(work),
            "--no-llm",
            "--quiet",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise SystemExit(
            f"reference agent failed on {pristine.name}: {done.stderr[-500:]}"
        )
    old_pos = {
        r["po_number"] for r in _rows(pristine, "SELECT po_number FROM purchase_orders")
    }
    old_alerts = {r["alert_id"] for r in _rows(pristine, "SELECT alert_id FROM alerts")}
    return {
        "orders": [
            r
            for r in _rows(work, "SELECT * FROM purchase_orders ORDER BY po_number")
            if r["po_number"] not in old_pos
        ],
        "alerts": [
            r["description"]
            for r in _rows(work, "SELECT * FROM alerts ORDER BY alert_id")
            if r["alert_id"] not in old_alerts
        ],
    }


def main():
    if not (REPO / "agent.py").exists():
        raise SystemExit(f"expected the reference agent at {REPO / 'agent.py'}")
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "scenarios").mkdir(parents=True)
    (OUT / "references").mkdir()
    DOCS.mkdir(parents=True, exist_ok=True)
    for pdf in sorted((REPO / "data").glob("*/*.pdf")):
        shutil.copyfile(pdf, DOCS / pdf.name)

    configs = []
    with tempfile.TemporaryDirectory(prefix="purchasing_") as td:
        tmp = Path(td)
        for item in _scenarios(tmp):
            task_id = item["task_id"]
            pristine = OUT / "scenarios" / f"{task_id}.sqlite"
            today = _prepare(item["source"], pristine)
            reference = _reference(pristine, tmp / f"{task_id}.sqlite")
            reference.update(task_id=task_id, current_date=today)
            (OUT / "references" / f"{task_id}.json").write_text(
                json.dumps(reference, indent=1)
            )
            configs.append(
                {
                    "task_id": task_id,
                    "kind": item["kind"],
                    "source_file": item["source"].name,
                    "current_date": today,
                    "tests": item["tests"],
                    "reference_orders": len(reference["orders"]),
                }
            )
            print(f"{task_id}: {len(reference['orders'])} reference orders")
    TASK_CONFIGS.parent.mkdir(parents=True, exist_ok=True)
    TASK_CONFIGS.write_text(json.dumps(configs, indent=4) + "\n")
    print(f"{len(configs)} tasks written to {TASK_CONFIGS.relative_to(ENV_DIR)}")


if __name__ == "__main__":
    main()
