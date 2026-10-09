"""Where the scenario data lives, for the evaluation scripts and tests.

The standard layout puts it inside the project (data/scenarios, data/policies, data/memos).
PROCUREMENT_DATA_DIR overrides it, e.g. to point at a folder of held-out scenarios. The agent itself
never needs this: it is given one scenario file with --scenario.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    if os.environ.get("PROCUREMENT_DATA_DIR"):
        return Path(os.environ["PROCUREMENT_DATA_DIR"])
    for candidate in (ROOT / "data", ROOT.parent / "data"):
        if (candidate / "scenarios").is_dir():
            return candidate
    return ROOT / "data"


def as_provided(path: Path) -> Path:
    """Undo an earlier agent run on a copied scenario: remove the agent's own purchase orders and
    alerts, so every evaluation starts from the state the scenario was provided in. (Running
    `agent.py --scenario` directly on a sample writes into it; this keeps results reproducible.)"""
    import sqlite3
    import sys
    sys.path.insert(0, str(ROOT))
    from procurement.alerts import ORDER
    from procurement.writer import PREFIX
    con = sqlite3.connect(path)
    try:
        with con:
            con.execute("DELETE FROM purchase_orders WHERE po_number LIKE ?", (PREFIX + "%",))
            for sev in ORDER:
                con.execute("DELETE FROM alerts WHERE description LIKE ?", (f"[{sev}]%",))
    finally:
        con.close()
    return path
