"""Layer 6 (act): write purchase orders and alerts into the scenario database.

Rules for writing:
  * Existing purchase orders are never changed or deleted, including ones the agent placed
    on an earlier run: a placed order is a commitment. The next run reads it back as
    incoming supply and only buys what is still missing. That is the agent loop.
  * New orders are numbered AGT-0001, AGT-0002, ... continuing after any earlier AGT orders.
  * Alerts describe the current situation, so the agent's previous alerts (recognisable by
    their severity tag) are replaced on each run. Alerts written by anyone else are kept.
  * Everything is written in one transaction: either all of it lands or none of it does.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .alerts import ORDER, Alert
from .decider import PlannedPO

PREFIX = "AGT-"


def write(path: str | Path, pos: list[PlannedPO], alerts: list[Alert]) -> list[str]:
    con = sqlite3.connect(str(path))
    try:
        with con:  # one transaction
            existing = [r[0] for r in con.execute("SELECT po_number FROM purchase_orders WHERE po_number LIKE ?",
                                                  (PREFIX + "%",))]
            nums = [int(m.group(1)) for n in existing if (m := re.fullmatch(PREFIX + r"(\d+)", n))]
            start = max(nums, default=0) + 1
            numbers = [f"{PREFIX}{i:04d}" for i in range(start, start + len(pos))]
            for num, p in zip(numbers, pos):
                con.execute(
                    "INSERT INTO purchase_orders (po_number, component_id, supplier_id, quantity, unit_price, "
                    "order_date, expected_delivery_date, rationale) VALUES (?,?,?,?,?,?,?,?)",
                    (num, p.component_id, p.supplier_id, p.quantity, p.unit_price,
                     p.order_date.isoformat(), p.expected_delivery_date.isoformat(), p.rationale))
            for sev in ORDER:
                con.execute("DELETE FROM alerts WHERE description LIKE ?", (f"[{sev}]%",))
            con.executemany("INSERT INTO alerts (description) VALUES (?)", [(a.render(),) for a in alerts])
        return numbers
    finally:
        con.close()
