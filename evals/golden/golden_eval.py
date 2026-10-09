"""Independent golden set: 17 small scenarios, each testing one policy rule, with expected answers
written from the policy and memo text outside this codebase.

It deliberately never reads the agent's rulebook (rules/policy_rules.json) or imports any of its
modules. It runs agent.py exactly as a user would, on a copy of each case, and checks what was
written to the database. So if the rulebook misreads the policy, this set disagrees, where the
policy scorecard (which reads the same rulebook as the agent) cannot. evals/golden/misreading_check.py
measures that: it plants misreadings in the rulebook and counts which evaluator notices.

The cases were drafted with an AI model from the supplied documents, then each expected answer was
checked against the policy text; the source clause is stored with every case in golden_cases.json.
They cover the clear rules, not the ambiguous ones (what proves receipt, borderline critical parts,
"significant volume"): those need the business's planners to write the expected decisions.

Usage:  python3 evals/golden/golden_eval.py            (prints a line per case, writes docs/golden.md)
        python3 evals/golden/golden_eval.py --repo <path to another copy of the project>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def rows(path: Path, sql: str) -> list[dict]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def shares(pos: list[dict]) -> tuple[float, float]:
    """(largest supplier's share, everyone else's share) of the units ordered."""
    by: dict[str, float] = {}
    for p in pos:
        by[p["supplier_id"]] = by.get(p["supplier_id"], 0) + float(p["quantity"])
    total = sum(by.values())
    top = max(by.values()) / total
    return top, 1 - top


def check(case: dict, db: Path) -> list[str]:
    """Compare what the agent wrote with the case's expected answer; return what differs."""
    pos = [r for r in rows(db, "SELECT * FROM purchase_orders ORDER BY po_number") if r["po_number"].startswith("AGT-")]
    alerts = "\n".join(r["description"] for r in rows(db, "SELECT description FROM alerts ORDER BY alert_id")).lower()
    rationale = "\n".join(p["rationale"] or "" for p in pos).lower()
    e, fails = case["expect"], []
    count = e.get("po_count", e.get("new_po_count"))
    if count is not None and len(pos) != count:
        fails.append(f"{len(pos)} new orders, expected {count}")
    if "po_count_min" in e and len(pos) < e["po_count_min"]:
        fails.append(f"{len(pos)} new orders, expected at least {e['po_count_min']}")
    qty = e.get("total_qty", e.get("new_total_qty"))
    total = sum(float(p["quantity"]) for p in pos)
    if qty is not None and abs(total - qty) > 1e-6:
        fails.append(f"bought {total:g} units, expected {qty:g}")
    used = {p["supplier_id"] for p in pos}
    allowed = set(e.get("only_suppliers", e.get("only_new_suppliers", [])))
    if allowed and not used <= allowed:
        fails.append(f"used {sorted(used)}, expected only {sorted(allowed)}")
    if used & set(e.get("never_suppliers", [])):
        fails.append(f"used a forbidden supplier: {sorted(used & set(e['never_suppliers']))}")
    if "delivery_date" in e and {p["expected_delivery_date"] for p in pos} != {e["delivery_date"]}:
        fails.append(f"delivery {sorted({p['expected_delivery_date'] for p in pos})}, expected {e['delivery_date']}")
    fails += [f"no order's reason mentions {w!r}" for w in e.get("rationale_contains", []) if w.lower() not in rationale]
    fails += [f"no alert mentions {w!r}" for w in e.get("alert_contains", []) if w.lower() not in alerts]
    if pos and "max_supplier_share" in e and shares(pos)[0] > e["max_supplier_share"] + 1e-9:
        fails.append(f"one supplier has {shares(pos)[0]:.0%}, cap {e['max_supplier_share']:.0%}")
    if pos and "min_secondary_share" in e and shares(pos)[1] + 1e-9 < e["min_secondary_share"]:
        fails.append(f"second supplier has {shares(pos)[1]:.0%}, needs {e['min_secondary_share']:.0%}")
    return fails


def run(repo: Path, quiet: bool = False) -> tuple[int, int, list[str]]:
    """Run every case against the agent in `repo`. Returns (passed, total, report lines)."""
    cases = json.loads((HERE / "golden_cases.json").read_text(encoding="utf-8"))
    lines, passed = [], 0
    with tempfile.TemporaryDirectory(prefix="golden_") as td:
        for c in cases:
            db = Path(td) / f"{c['id']}.sqlite"
            shutil.copyfile(HERE / "cases" / db.name, db)        # always a copy
            done = subprocess.run([sys.executable, str(repo / "agent.py"), "--scenario", str(db), "--no-llm", "--quiet"],
                                  cwd=repo, capture_output=True, text=True)
            fails = [f"agent exited {done.returncode}: {(done.stderr or done.stdout).strip()[-200:]}"] if done.returncode \
                else check(c, db)
            passed += not fails
            case_lines = [f"{'PASS' if not fails else 'FAIL'} {c['id']}: {c['title']}"]
            case_lines += [f"   - {f}" for f in fails] + ([f"   source: {c['source']}"] if fails else [])
            lines += case_lines
            if not quiet:
                print("\n".join(case_lines), flush=True)
    return passed, len(cases), lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=str(ROOT), help="the project to test (default: this one)")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    passed, total, lines = run(repo)
    print(f"\n{passed} of {total} independent golden cases passed")
    if repo == ROOT:
        (ROOT / "docs" / "golden.md").write_text("# Independent golden set\n\n" + "\n".join(f"    {l}" for l in lines)
                                                  + f"\n\n{passed} of {total} cases passed.\n", encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
