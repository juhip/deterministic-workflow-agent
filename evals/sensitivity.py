"""Sensitivity: how much would the results change if the business answers an open question differently?

Each alternative changes one rulebook entry (never the code), reruns the six samples on copies,
and reports what moved: orders whose supplier or quantity changed, spend, late production orders
and how many orders release without a person.

Usage:  python3 evals/sensitivity.py        (prints a table, writes docs/sensitivity.md)
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from procurement.db import load_state                    # noqa: E402
from procurement.pipeline import run                     # noqa: E402
from procurement.rules import DEFAULT_RULES_PATH, Rulebook   # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datadir import as_provided, data_dir   # noqa: E402

DATA = data_dir() / "scenarios"


def _rule(raw, rule_type):
    return next(r for r in raw["rules"] if r["type"] == rule_type)


def mexico_international(raw):
    dom = _rule(raw, "domestic_definition")["params"]
    dom["countries"] = [c for c in dom["countries"] if c.lower() != "mexico"]


def schedule_wins(raw):
    raw["settings"]["when_rule_conflicts_with_schedule"] = "protect_schedule"


def freeze_needs_prior_order(raw):
    _rule(raw, "supplier_qualification_freeze")["params"]["evidence_of_prior_receipt"]["relationship_tier_in"] = []


def significant_above_10k(raw):
    raw["settings"]["strategic_shift_significant_above"] = 10000


def release_limit_10k(raw):
    raw["settings"]["approval_exceptions"]["max_order_value"] = 10000


def critical_hold_off(raw):
    """Holding every critical part is a caution choice, not the policy's; switch that one exception off."""
    raw["settings"]["approval_exceptions"]["critical_part"] = False


def single_supplier_critical_only(raw):
    """The policy's two-supplier rule (section 4) covers critical parts only."""
    raw["settings"]["approval_exceptions"]["single_allowed_supplier"] = "critical_only"


def critical_single_sourced_only(raw):
    """Hold a critical part only when it has one allowed supplier (model-only labels stay held via ai_flag)."""
    raw["settings"]["approval_exceptions"]["critical_part"] = "single_sourced_only"


def both_caution_holds_narrowed(raw):
    single_supplier_critical_only(raw)
    critical_single_sourced_only(raw)


def narrowed_and_significant_above_10k(raw):
    both_caution_holds_narrowed(raw)
    significant_above_10k(raw)


# (open question or assumption, alternative rule or answer, change to the rulebook)
ALTERNATIVES = [
    ("Q1 Memo vs deadline", "the customer deadline wins", schedule_wins),
    ("Q2 Release limit and hold relaxations", "$10,000 instead of $5,000", release_limit_10k),
    ("Q2 Release limit and hold relaxations", "single-supplier hold for critical parts only", single_supplier_critical_only),
    ("Q2 Release limit and hold relaxations", "critical-part hold only when single-sourced", critical_single_sourced_only),
    ("Q2 Release limit and hold relaxations", "both holds narrowed", both_caution_holds_narrowed),
    ("Q2 Release limit and hold relaxations", "stop holding every critical part (the policy does not require it)", critical_hold_off),
    ("Q3 Significant strategic suppliers", "only orders over $10,000", significant_above_10k),
    ("Q2 + Q3 together", "both holds narrowed, and significant means over $10,000", narrowed_and_significant_above_10k),
    ("Q4 Proof of receipt for circuit-board", "only a prior order counts as receipt", freeze_needs_prior_order),
    ("Mexico (assumption, not a question)", "international, as the database says", mexico_international),
]


def summarize(rules: Rulebook) -> dict:
    out = {"orders": set(), "spend": 0.0, "late": 0, "released": 0, "count": 0}
    for src in sorted(DATA.glob("scenario_*.sqlite")):
        work = Path(tempfile.mkdtemp()) / src.name            # always a copy
        shutil.copy(src, work)
        as_provided(work)
        res = run(load_state(work), rules)
        out["orders"] |= {(src.stem, p.component_id, p.supplier_id, p.quantity) for p in res.pos}
        out["spend"] += sum(p.total for p in res.pos)
        out["late"] += sum(1 for s in res.statuses if not s.on_time)
        out["released"] += sum(1 for r in res.releases if not r.needs_approval)
        out["count"] += len(res.pos)
    return out


def main() -> int:
    raw = json.loads(Path(DEFAULT_RULES_PATH).read_text())
    base = summarize(Rulebook(raw))
    lines = ["| Open question | Alternative answer | Parts bought differently | Spend | Late production orders | Released without a person |",
             "|---|---|---|---|---|---|",
             f"| (today's defaults) | | | ${base['spend']:,.2f} | {base['late']} | {base['released']} of {base['count']} |"]
    for question, answer, change in ALTERNATIVES:
        alt_raw = copy.deepcopy(raw)
        change(alt_raw)
        alt = summarize(Rulebook(alt_raw))
        diff = alt["orders"] ^ base["orders"]                  # orders added, dropped or re-sourced
        changed = len({(scn, comp) for scn, comp, _, _ in diff})
        lines.append(f"| {question} | {answer} | {changed} | ${alt['spend']:,.2f} ({alt['spend'] - base['spend']:+,.2f}) | "
                     f"{alt['late']} ({alt['late'] - base['late']:+d}) | {alt['released']} of {alt['count']} |")
    report = "\n".join(lines)
    print(report)
    (ROOT / "docs" / "sensitivity.md").write_text("# Sensitivity to the open questions\n\n" + report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
