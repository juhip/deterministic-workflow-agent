"""Read a scenario database into typed objects.

This module only reads. Writing results is a separate layer (writer.py) so the code that
looks at the world can never accidentally change it.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import (CatalogEntry, Component, ExistingPO, ProductionOrder,
                     ScenarioState, Supplier)


def parse_date(value) -> date | None:
    """Accept 'YYYY-MM-DD' (optionally followed by a time). Blank -> None."""
    if value is None or str(value).strip() == "":
        return None
    return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()


def split_list(value) -> tuple[str, ...]:
    """'ISO-9001,IEC-62368' -> ('ISO-9001', 'IEC-62368'). NULL, '' and '0' -> ()."""
    if value is None:
        return ()
    parts = (p.strip() for p in str(value).replace(";", ",").split(","))
    return tuple(p for p in parts if p and p != "0")


def copy_scenario(src: str | Path, dest_dir: str | Path) -> Path:
    """Copy a scenario DB somewhere disposable. Tests and evals always run on copies."""
    dest = Path(dest_dir) / Path(src).name
    shutil.copyfile(src, dest)
    return dest


def load_state(path: str | Path) -> ScenarioState:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"scenario database not found: {path}")
    # mode=ro: the loader physically cannot modify the file
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    issues: list[str] = []
    try:
        # TRAP: current_date must be quoted. Unquoted, SQLite treats it as its built-in
        # CURRENT_DATE function and returns the computer's real date, not the scenario's.
        cfg = con.execute('SELECT "current_date" AS d, scenario_description AS s FROM scenario_config').fetchone()
        if cfg is None or parse_date(cfg["d"]) is None:
            raise ValueError("scenario_config has no current_date; cannot plan without knowing 'today'")

        components = {
            r["component_id"]: Component(
                r["component_id"], r["name"], r["description"] or "", r["category"] or "",
                r["unit_of_measure"] or "", bool(r["is_hazardous"]), split_list(r["requires_certification"]))
            for r in con.execute("SELECT * FROM components")}

        suppliers = {
            r["supplier_id"]: Supplier(
                r["supplier_id"], r["name"], r["country"] or "", bool(r["is_domestic"]),
                split_list(r["certifications"]), (r["sustainability_rating"] or "").strip(),
                (r["relationship_tier"] or "").strip(), bool(r["on_approved_list"]), r["notes"] or "")
            for r in con.execute("SELECT * FROM suppliers")}

        catalog = []
        for r in con.execute("SELECT * FROM supplier_catalog ORDER BY component_id, supplier_id"):
            if r["supplier_id"] not in suppliers:
                issues.append(f"Catalog lists {r['component_id']} from unknown supplier {r['supplier_id']}; ignored.")
                continue
            catalog.append(CatalogEntry(r["supplier_id"], r["component_id"], float(r["unit_price"]),
                                        int(r["lead_time_days"]), int(r["minimum_order_qty"] or 1), r["notes"] or ""))

        bom: dict[str, dict[str, float]] = {}
        for r in con.execute("SELECT * FROM bom"):
            if r["component_id"] not in components:
                issues.append(f"BOM for {r['product_id']} references unknown component {r['component_id']}.")
            bom.setdefault(r["product_id"], {})[r["component_id"]] = float(r["quantity_per"])

        inventory = {r["component_id"]: float(r["quantity_on_hand"]) for r in con.execute("SELECT * FROM inventory")}
        products = {r["product_id"]: dict(r) for r in con.execute("SELECT * FROM products")}

        schedule = []
        for r in con.execute("SELECT * FROM production_schedule ORDER BY materials_needed_by, order_id"):
            need_by = parse_date(r["materials_needed_by"])
            if need_by is None:
                issues.append(f"Production order {r['order_id']} has no materials_needed_by date; skipped.")
                continue
            if r["product_id"] not in bom:
                issues.append(f"Production order {r['order_id']}: product {r['product_id']} has no bill of materials.")
            schedule.append(ProductionOrder(r["order_id"], r["product_id"], int(r["quantity"]),
                                            r["customer"] or "", need_by))

        existing_pos = [
            ExistingPO(r["po_number"], r["component_id"], r["supplier_id"], float(r["quantity"]),
                       r["unit_price"], parse_date(r["order_date"]), parse_date(r["expected_delivery_date"]),
                       r["rationale"] or "")
            for r in con.execute("SELECT * FROM purchase_orders ORDER BY po_number")]

        alert_count = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    finally:
        con.close()

    return ScenarioState(str(path), parse_date(cfg["d"]), cfg["s"] or "", products, components, bom,
                         suppliers, catalog, inventory, schedule, existing_pos, alert_count, issues)
