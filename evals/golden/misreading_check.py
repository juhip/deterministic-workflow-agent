"""Can the evaluators catch a misread policy? Plant one misreading at a time and see who notices.

The risk the policy scorecard cannot cover is a rule the rulebook gets wrong: the agent and the
scorecard read the same rulebook, so both would agree with the mistake. This check plants 15
realistic misreadings in the rulebook (a wrong threshold, a wrong date, a rule left out), one at a
time, each in a temporary copy of the project, and runs two evaluators on each:

  the independent golden set   expected answers written from the policy text, never the rulebook
  the policy scorecard          reads the same rulebook as the agent

A misreading counts as caught if the evaluator reports a failure. Nothing in this project is changed.
Takes about two minutes.

Usage:  python3 evals/golden/misreading_check.py      (prints a table, writes docs/misreading_check.md)
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from golden_eval import run as run_golden   # noqa: E402


def rule(raw: dict, rid: str) -> dict:
    return next(r for r in raw["rules"] if r["id"] == rid)


def drop(raw: dict, rid: str) -> None:
    raw["rules"] = [r for r in raw["rules"] if r["id"] != rid]


def no_sensor_ics(raw: dict) -> None:
    p = rule(raw, "critical-components")["params"]
    p["categories"] = [c for c in p["categories"] if "sensing" not in c["label"].lower()]


MISREADINGS = [  # (what was misread, how the rulebook gets it wrong)
    ("Critical-part price premium read as 30% (policy: 45%)",
     lambda r: rule(r, "domestic-preference")["params"].update(premium_threshold_critical=0.30)),
    ("Normal-part price premium read as 45% (policy: 30%)",
     lambda r: rule(r, "domestic-preference")["params"].update(premium_threshold=0.45)),
    ("Mexico not counted as domestic (policy: US and Mexico)",
     lambda r: rule(r, "domestic-definition")["params"].update(countries=["United States", "USA", "US"])),
    ("Magnet cap read as 65% (memo: 60%)",
     lambda r: rule(r, "concentration-magnets")["params"].update(max_share=0.65)),
    ("Magnet memo start read as July 1 (memo: May 18)",
     lambda r: rule(r, "concentration-magnets").update(effective_from="2026-07-01")),
    ("Air-freight window read as ending Dec 31 (memo: Nov 2)",
     lambda r: rule(r, "air-freight").update(effective_to="2026-12-31")),
    ("Air freight read as cutting 5 days (memo: 10)",
     lambda r: rule(r, "air-freight")["params"].update(lead_time_reduction_days=5)),
    ("Approved Supplier List rule left out",
     lambda r: drop(r, "approved-list")),
    ("IEC-62368 certification not required for power parts",
     lambda r: rule(r, "cert-power-supply")["params"].update(certifications=["ISO-9001"])),
    ("ISO-9001 not required for electronic parts",
     lambda r: drop(r, "cert-electronics")),
    ("Sensing ICs left off the critical-parts list", no_sensor_ics),
    ("Strategic-supplier band read as 5% (policy: 12%)",
     lambda r: rule(r, "strategic-suppliers")["params"].update(max_savings_to_stay=0.05)),
    ("Sustainability price band read as 3% (policy: 8%)",
     lambda r: rule(r, "sustainability")["params"].update(comparable_price_pct=0.03)),
    ("Sourcing Manager level read as $100,000 (policy: $40,000)",
     lambda r: rule(r, "approval-thresholds")["params"]["approval_levels"][0].update(above=100000)),
    ("VP of Supply Chain level read as $250,000 (policy: $120,000)",
     lambda r: rule(r, "approval-thresholds")["params"]["approval_levels"][1].update(above=250000)),
]


def main() -> int:
    base = json.loads((ROOT / "rules" / "policy_rules.json").read_text(encoding="utf-8"))
    lines = ["| Planted misreading | Golden set | Policy scorecard |", "|---|---|---|"]
    golden_caught = scorecard_caught = 0
    for what, mutate in MISREADINGS:
        work = Path(tempfile.mkdtemp(prefix="proc_misread_"))
        repo = work / "project"
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.png", "cache"))
        raw = copy.deepcopy(base)
        mutate(raw)
        (repo / "rules" / "policy_rules.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
        passed, total, _ = run_golden(repo, quiet=True)
        scorecard = subprocess.run([sys.executable, str(repo / "evals" / "policy_scorecard.py")],
                                   cwd=repo, capture_output=True, text=True)
        g = passed < total
        s = "No checks failed" not in scorecard.stdout
        golden_caught += g
        scorecard_caught += s
        lines.append(f"| {what} | {'caught' if g else 'missed'} | {'caught' if s else 'missed'} |")
        print(lines[-1], flush=True)
        shutil.rmtree(work, ignore_errors=True)
    n = len(MISREADINGS)
    summary = f"The golden set caught {golden_caught} of {n}; the policy scorecard caught {scorecard_caught} of {n}."
    print("\n" + summary)
    (ROOT / "docs" / "misreading_check.md").write_text(
        "# Can the evaluators catch a misread policy?\n\n" + "\n".join(lines) + "\n\n" + summary + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
