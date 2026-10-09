"""Stress test: run the agent on 140 varied scenarios, the way a user will.

Each case starts from a sample and changes what a held-out scenario might change: the date
(every two weeks from late 2025 to mid 2027, across every memo window), the ID formats, the
structure (suppliers, demand, products, stock) and missing optional data. The agent is run
exactly as documented: `python3 agent.py --scenario <file>`, from a different folder,
with no model. For every case it checks that the agent

  exits cleanly               no crash, nothing on stderr, within the time limit
  passes the policy scorecard every clause of the policy and memos (evals/policy_scorecard.py)
  is deterministic            two fresh copies give identical orders and alerts
  is safe to rerun            covered by the scorecard: a second run buys nothing more

Every case is built from the six samples, so it reuses their parts, suppliers and memos:
this is held-out-style data, not truly held-out data.

Everything runs on copies; nothing under data/ is changed.

Usage:  python3 evals/stress_test.py            (prints a summary, writes docs/stress_test.md)
"""
from __future__ import annotations

import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_scenarios as gen                      # noqa: E402
import policy_scorecard as ps                    # noqa: E402
from procurement.rules import Rulebook           # noqa: E402

SAMPLES = sorted(gen.DATA.glob("scenario_*.sqlite"))
TIME_LIMIT_S = 30


# ---- ways a held-out scenario might differ -------------------------------------------------
def rename_ids(style: str):
    """Rename every ID in a given style (e.g. JOB-6030)."""
    def fmt(kind: str, i: int) -> str:
        return {"numeric": f"{kind[0].upper()}{900 + i}",
                "lower": f"{kind}_{i:03d}",
                "long": f"{kind.upper()}-2026-{i:05d}"}[style]
    def apply(con):
        maps = {"order": "SELECT order_id FROM production_schedule", "part": "SELECT component_id FROM components",
                "supplier": "SELECT supplier_id FROM suppliers", "product": "SELECT product_id FROM products",
                "po": "SELECT po_number FROM purchase_orders"}
        maps = {k: {old: fmt(k, i) for i, old in enumerate(sorted(r[0] for r in con.execute(q)))} for k, q in maps.items()}
        for table, cols in {"production_schedule": [("order_id", "order"), ("product_id", "product")],
                            "products": [("product_id", "product")],
                            "bom": [("product_id", "product"), ("component_id", "part")],
                            "components": [("component_id", "part")], "inventory": [("component_id", "part")],
                            "suppliers": [("supplier_id", "supplier")],
                            "supplier_catalog": [("supplier_id", "supplier"), ("component_id", "part")],
                            "purchase_orders": [("po_number", "po"), ("component_id", "part"), ("supplier_id", "supplier")]}.items():
            for col, which in cols:
                con.executemany(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", [(n, o) for o, n in maps[which].items()])
    apply.__name__ = f"ids_{style}"
    return apply


def scale_demand(factor: float):
    def apply(con):
        con.execute("UPDATE production_schedule SET quantity = MAX(1, CAST(quantity * ? AS INTEGER))", (factor,))
    apply.__name__ = f"demand_x{factor:g}"
    return apply


def stock_covers_everything(con):
    con.execute("UPDATE inventory SET quantity_on_hand = quantity_on_hand + 100000")


def no_stock(con):
    con.execute("UPDATE inventory SET quantity_on_hand = 0")


def drop_random_supplier(rng):
    def apply(con):
        ids = [r[0] for r in con.execute("SELECT supplier_id FROM suppliers ORDER BY supplier_id")]
        victim = rng.choice(ids)
        con.execute("DELETE FROM supplier_catalog WHERE supplier_id = ?", (victim,))
        con.execute("DELETE FROM suppliers WHERE supplier_id = ?", (victim,))
    apply.__name__ = "drop_a_supplier"
    return apply


def unbuyable_part(con):
    """One part no supplier sells any more: the agent must report it, not crash."""
    part = con.execute("SELECT component_id FROM bom ORDER BY component_id LIMIT 1").fetchone()[0]
    con.execute("DELETE FROM supplier_catalog WHERE component_id = ?", (part,))
    con.execute("UPDATE inventory SET quantity_on_hand = 0 WHERE component_id = ?", (part,))


def empty_schedule(con):
    con.execute("DELETE FROM production_schedule")


def missing_optional_text(con):
    con.execute("UPDATE suppliers SET notes = NULL")
    con.execute("UPDATE supplier_catalog SET notes = NULL")
    con.execute("UPDATE components SET description = NULL")
    con.execute("UPDATE products SET unit_price = NULL")


def need_date_already_passed(con):
    today = con.execute('SELECT "current_date" FROM scenario_config').fetchone()[0][:10]
    first = con.execute("SELECT order_id FROM production_schedule ORDER BY materials_needed_by LIMIT 1").fetchone()
    if first:
        past = (date.fromisoformat(today) - timedelta(days=3)).isoformat()
        con.execute("UPDATE production_schedule SET materials_needed_by = ? WHERE order_id = ?", (past, first[0]))


def no_existing_orders(con):
    con.execute("DELETE FROM purchase_orders")


def cases(rng: random.Random):
    """(sample, new date or None, list of changes, label)."""
    out = []
    # every two weeks across every memo window, each with a different sample and ID style
    day, styles = date(2025, 11, 3), ["numeric", "lower", "long", None]
    i = 0
    while day <= date(2027, 7, 5):
        style = styles[i % len(styles)]
        changes = []
        if style:
            changes.append(rename_ids(style))
        out.append((SAMPLES[i % len(SAMPLES)], day.isoformat(), changes, f"date {day}" + (f", IDs {style}" if style else "")))
        day += timedelta(days=14); i += 1
    # structural and data-quality changes on every sample
    structural = [scale_demand(0.2), scale_demand(5), scale_demand(40), stock_covers_everything, no_stock,
                  drop_random_supplier(rng), unbuyable_part, empty_schedule, missing_optional_text,
                  need_date_already_passed, no_existing_orders, gen.add_product]
    for sample in SAMPLES:
        for change in structural:
            out.append((sample, None, [change], change.__name__))
    # a few random combinations
    for _ in range(24):
        combo = rng.sample(structural, 2) + [rename_ids(rng.choice(["numeric", "lower", "long"]))]
        when = (date(2025, 12, 1) + timedelta(days=rng.randrange(0, 600))).isoformat()
        out.append((rng.choice(SAMPLES), when, combo, " + ".join(c.__name__ for c in combo) + f" @ {when}"))
    return out


# ---- running the agent the way a user will --------------------------------------------
def build(sample: Path, when: str | None, changes, dest: Path) -> Path:
    gen.make_variant(sample, dest, when)              # renames IDs to JOB-/ITM-/VN- and moves the date
    con = sqlite3.connect(dest)
    for change in changes:
        change(con)
    con.commit()
    con.close()
    return dest


def run_agent(path: Path, cwd: Path) -> tuple[int, str, float]:
    t = time.time()
    p = subprocess.run([sys.executable, str(ROOT / "agent.py"), "--scenario", str(path)], cwd=cwd,
                       capture_output=True, text=True, timeout=TIME_LIMIT_S,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(cwd)})       # no model settings, no network
    return p.returncode, p.stderr.strip(), time.time() - t


def snapshot(path: Path):
    con = sqlite3.connect(path)
    try:
        return (con.execute("SELECT component_id, supplier_id, quantity, unit_price, order_date, expected_delivery_date, "
                            "rationale FROM purchase_orders ORDER BY po_number").fetchall(),
                con.execute("SELECT description FROM alerts ORDER BY alert_id").fetchall())
    finally:
        con.close()


def main() -> int:
    rng = random.Random(7)
    rules = Rulebook.load()
    work = Path(tempfile.mkdtemp(prefix="proc_stress_"))
    elsewhere = Path(tempfile.mkdtemp(prefix="proc_cwd_"))      # the agent is run from an unrelated folder
    problems, times, n = [], [], 0
    for k, (sample, when, changes, label) in enumerate(cases(rng)):
        n += 1
        a = build(sample, when, changes, work / f"case{k:03d}_a.sqlite")
        b = work / f"case{k:03d}_b.sqlite"
        shutil.copy(a, b)
        name = f"{sample.stem} | {label}"
        try:
            code, err, secs = run_agent(a, elsewhere)
            times.append(secs)
            if code != 0 or err:
                problems.append(f"{name}: exit {code}: {err[-300:]}")
                continue
            run_agent(b, elsewhere)
            if snapshot(a) != snapshot(b):
                problems.append(f"{name}: two identical copies gave different results")
            before = {key: len(c.failed) for key, c in ps.CHECKS.items()}
            fresh = work / f"case{k:03d}_c.sqlite"
            build(sample, when, changes, fresh)
            ps.grade(fresh, name, rules)
            for key, c in ps.CHECKS.items():
                problems += c.failed[before[key]:]
        except subprocess.TimeoutExpired:
            problems.append(f"{name}: took longer than {TIME_LIMIT_S}s")
        except Exception as e:                                    # noqa: BLE001
            problems.append(f"{name}: {type(e).__name__}: {str(e)[:300]}")
    checks = sum(c.checked for c in ps.CHECKS.values())
    lines = [f"Scenarios: {n}", f"Scorecard checks: {checks}", f"Problems: {len(problems)}",
             f"Agent run time: median {sorted(times)[len(times) // 2]:.2f}s, slowest {max(times):.2f}s"]
    report = "\n".join(lines) + ("\n\nProblems:\n" + "\n".join(f"- {p}" for p in problems) if problems else "")
    print(report)
    (ROOT / "docs" / "stress_test.md").write_text("# Stress test (140 scenarios built from the samples)\n\n" + report + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
