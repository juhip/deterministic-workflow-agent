"""Make held-out-style scenarios from the samples, to test that the agent generalizes.

New scenarios will not reuse the samples' IDs or dates (an alert like "Cannot meet May 20 start
date for JOB-6030" refers to neither), so held-out data will differ in exactly those ways. Each
variant here:

  renames every ID   production orders -> JOB-6xxx, parts -> ITM-44xx (the memos' own numbering),
                     suppliers -> VN-5xx, products -> MOD-3xx, existing orders -> OPEN-xxx
  moves the dates    today, every need-by date and every existing order shift together, so the
                     scenario lands on the other side of a memo window (or before the policy began)

Those keep the samples' structure. STRUCTURAL variants also change the structure, so the agent
meets situations the samples never show:

  scale demand     production quantities x40, so single orders cross the $40,000 and $120,000
                   approval levels that no sample reaches
  drop a supplier  the approved supplier with the widest catalog disappears, so parts must be
                   re-sourced (or reported as unbuyable)
  add a product    a new product (a copy of the largest bill of materials) and a new customer
                   order compete for the same parts

The policy scorecard grades every variant (evals/policy_scorecard.py).
"""
from __future__ import annotations

import shutil
import sys
import sqlite3
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datadir import as_provided, data_dir   # noqa: E402

DATA = data_dir() / "scenarios"

# (sample, new "today" or None to keep it, what the variant tests)
VARIANTS = [
    ("scenario_01_baseline.sqlite", None, "new ID formats only"),
    ("scenario_05_competing_demand.sqlite", "2026-03-02", "before the magnet memo and the air-freight window"),
    ("scenario_03_tight_timeline.sqlite", "2026-08-24", "air freight open, before the circuit-board freeze"),
    ("scenario_02_partial_procurement.sqlite", "2026-10-28", "air freight in its last days, freeze active, existing orders"),
    ("scenario_04_low_inventory.sqlite", "2025-12-14", "before the procurement policy took effect"),
    ("scenario_06_simple.sqlite", "2027-03-08", "well after every memo window"),
]


def scale_demand(con, factor=40):
    con.execute("UPDATE production_schedule SET quantity = quantity * ?", (factor,))


def drop_widest_supplier(con):
    """Remove the approved supplier that sells the most parts (chosen from the data, not by ID)."""
    sup = con.execute("SELECT s.supplier_id FROM suppliers s JOIN supplier_catalog c USING (supplier_id) "
                      "WHERE s.on_approved_list = 1 GROUP BY s.supplier_id ORDER BY COUNT(*) DESC, s.supplier_id LIMIT 1").fetchone()[0]
    con.execute("DELETE FROM supplier_catalog WHERE supplier_id = ?", (sup,))
    con.execute("DELETE FROM suppliers WHERE supplier_id = ?", (sup,))


def add_product(con):
    """Copy the product with the largest bill of materials as a new product with a new customer order."""
    src = con.execute("SELECT product_id FROM bom GROUP BY product_id ORDER BY COUNT(*) DESC, product_id LIMIT 1").fetchone()[0]
    new = "MOD-NEW"
    cols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
    row = dict(zip(cols, con.execute("SELECT * FROM products WHERE product_id = ?", (src,)).fetchone()))
    row.update(product_id=new, name=f"{row['name']} (new variant)")
    con.execute(f"INSERT INTO products ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(row.values()))
    con.execute("INSERT INTO bom (product_id, component_id, quantity_per) "
                "SELECT ?, component_id, quantity_per FROM bom WHERE product_id = ?", (new, src))
    last = con.execute("SELECT MAX(materials_needed_by), MAX(quantity) FROM production_schedule").fetchone()
    need = (date.fromisoformat(last[0][:10]) + timedelta(days=7)).isoformat()
    con.execute("INSERT INTO production_schedule (order_id, product_id, quantity, customer, materials_needed_by) "
                "VALUES (?, ?, ?, ?, ?)", ("JOB-NEW", new, last[1], "Kittiwake Offshore", need))


# (sample, new "today" or None, what the variant tests, structural change)
STRUCTURAL = [
    ("scenario_04_low_inventory.sqlite", None, "demand x40: orders cross the $40,000 and $120,000 approval levels", scale_demand),
    ("scenario_01_baseline.sqlite", None, "the approved supplier with the widest catalog removed", drop_widest_supplier),
    ("scenario_05_competing_demand.sqlite", None, "a new product and customer order compete for the same parts", add_product),
]


def _rows(con, sql):
    return [r[0] for r in con.execute(sql)]


def make_variant(src: Path, dest: Path, new_today: str | None, mutate=None) -> Path:
    shutil.copy(src, dest)
    as_provided(dest)
    con = sqlite3.connect(dest)
    maps = {
        "order": {o: f"JOB-{6030 + i}" for i, o in enumerate(sorted(_rows(con, "SELECT order_id FROM production_schedule")))},
        "part": {c: f"ITM-{4401 + i}" for i, c in enumerate(sorted(_rows(con, "SELECT component_id FROM components")))},
        "supplier": {s: f"VN-{501 + i}" for i, s in enumerate(sorted(_rows(con, "SELECT supplier_id FROM suppliers")))},
        "product": {p: f"MOD-{301 + i}" for i, p in enumerate(sorted(_rows(con, "SELECT product_id FROM products")))},
        "po": {p: f"OPEN-{101 + i}" for i, p in enumerate(sorted(_rows(con, "SELECT po_number FROM purchase_orders")))},
    }
    columns = {   # table -> [(column, which map)]
        "production_schedule": [("order_id", "order"), ("product_id", "product")],
        "products": [("product_id", "product")],
        "bom": [("product_id", "product"), ("component_id", "part")],
        "components": [("component_id", "part")],
        "inventory": [("component_id", "part")],
        "suppliers": [("supplier_id", "supplier")],
        "supplier_catalog": [("supplier_id", "supplier"), ("component_id", "part")],
        "purchase_orders": [("po_number", "po"), ("component_id", "part"), ("supplier_id", "supplier")],
    }
    for table, cols in columns.items():
        for col, which in cols:
            con.executemany(f'UPDATE {table} SET {col} = ? WHERE {col} = ?',
                            [(new, old) for old, new in maps[which].items()])
    if new_today:
        old_today = date.fromisoformat(con.execute('SELECT "current_date" FROM scenario_config').fetchone()[0][:10])
        shift = (date.fromisoformat(new_today) - old_today).days
        move = lambda v: (date.fromisoformat(v[:10]) + timedelta(days=shift)).isoformat() if v else v
        con.execute('UPDATE scenario_config SET "current_date" = ?', (new_today,))
        for table, cols in (("production_schedule", ["materials_needed_by"]),
                            ("purchase_orders", ["order_date", "expected_delivery_date"])):
            for col in cols:
                for rowid, v in con.execute(f"SELECT rowid, {col} FROM {table}").fetchall():
                    con.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (move(v), rowid))
    if mutate:
        mutate(con)
    con.commit()
    con.close()
    return dest


def make_all(out_dir: Path) -> list[tuple[Path, str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for name, today, why in VARIANTS:
        stem = name.replace(".sqlite", "") + ("_renamed" + (f"_{today}" if today else ""))
        made.append((make_variant(DATA / name, out_dir / f"{stem}.sqlite", today), why))
    return made


def make_structural(out_dir: Path) -> list[tuple[Path, str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return [(make_variant(DATA / name, out_dir / f"{name.replace('.sqlite', '')}_{fn.__name__}.sqlite", today, fn), why)
            for name, today, why, fn in STRUCTURAL]


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "generated")
    for path, why in make_all(out) + make_structural(out):
        print(f"{path}  ({why})")
