"""Outcome checks: did the business get the right result, not only did each order follow the rules?

The policy scorecard checks every order against every clause. This checks outcomes, in two ways:

  outcome metrics   on the six samples: customers on time, why each late order is late, what the
                    policy costs over the cheapest allowed supplier, units bought beyond need, and
                    how many orders a planner must review
  checks under change   no answer key is needed: change a scenario in a known way and check that
                    the result moves the right way (more stock never means more orders, a renamed
                    ID never changes a decision, adding supply never makes an order later, ...)

It is a report, not a gate: two checks fail today, and both are documented limitations in the README
(Limitations): a late existing order hides an on-time supplier, and the agent replaces a person's
alert written in its own [INFO] / [ACTION] format. Everything runs on copies of the samples.

Usage:  python3 evals/outcomes.py        (prints the report, writes docs/outcomes.md)
"""
from __future__ import annotations

import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from procurement.db import load_state                    # noqa: E402
from procurement.options import build_options            # noqa: E402
from procurement.pipeline import run                     # noqa: E402
from procurement.rules import Rulebook                   # noqa: E402

from datadir import as_provided, data_dir                # noqa: E402
from gen_scenarios import make_variant                   # noqa: E402
from policy_scorecard import REASONS, hold_reasons       # noqa: E402

DATA = data_dir() / "scenarios"
RULES = Rulebook.load()
TMP = Path(tempfile.mkdtemp(prefix="proc_outcomes_"))


def copy_of(src: Path, name: str) -> Path:
    """A fresh copy of a sample, as provided (never the sample itself)."""
    dest = TMP / name
    shutil.copy(src, dest)
    as_provided(dest)
    return dest


def plan(path: Path, explore: bool = False):
    state = load_state(path)
    return state, run(state, RULES, explore=explore)


def lateness(result) -> tuple[int, int]:
    """(late production orders, total days late); an order never fully supplied counts as late."""
    late = [s for s in result.statuses if not s.on_time]
    return len(late), sum(s.late_days or 0 for s in late)


def units_by_part(result) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in result.pos:
        out[p.component_id] = out.get(p.component_id, 0) + p.quantity
    return out


def edit(path: Path, sql: str, params=()) -> None:
    con = sqlite3.connect(path)
    with con:
        con.execute(sql, params)
    con.close()


# ---- outcome metrics on the six samples -------------------------------------------------------
def outcome_metrics(samples: list[Path]) -> dict:
    m = {"orders": 0, "on_time": 0, "late": [], "spend": 0.0, "premium": 0.0, "premium_parts": 0,
         "extra_units": 0, "extra_cost": 0.0, "pos": 0, "released": 0, "policy_holds": 0, "caution_holds": 0}
    for src in samples:
        state, res = plan(copy_of(src, src.name), explore=True)
        rules, _ = RULES.base_policy_in_force(state.current_date)
        m["orders"] += len(res.statuses)
        m["on_time"] += sum(s.on_time for s in res.statuses)

        # why is each late order late? unavoidable / a rule forced it (and it was escalated) / avoidable
        for s in res.statuses:
            if s.on_time:
                continue
            kind = "unavoidable: no allowed supplier is fast enough"
            for cid, _ in s.limiting:
                opts = [o for o in build_options(state, rules, res.tags[cid], cid) if o.eligible]
                if not opts:
                    kind = "unavoidable: no allowed supplier sells a part"
                    continue
                if min(o.arrival for o in opts) <= s.order.materials_needed_by:
                    dec = res.decisions.get(cid)
                    forced = dec is not None and (dec.conflicts or dec.alternative)
                    kind = ("a rule forced it, and it was escalated" if forced
                            else "AVOIDABLE: an allowed supplier could have made the date")
                    if not forced:
                        break
            m["late"].append((src.stem, s.order.customer, s.late_days, kind))

        # what the policy costs: spend above the cheapest allowed supplier that is on time for each part
        for cid in {p.component_id for p in res.pos}:
            pos = [p for p in res.pos if p.component_id == cid]
            need = sum(q for p in pos for _, q in p.serves)
            need_by = min((o.materials_needed_by for o in state.schedule
                           if o.order_id in {oid for p in pos for oid, _ in p.serves}), default=state.current_date)
            opts = [o for o in build_options(state, rules, res.tags[cid], cid) if o.eligible]
            on_time = [o for o in opts if o.arrival <= need_by] or \
                      [o for o in opts if o.arrival == min(x.arrival for x in opts)]
            cheapest = min(math.ceil(max(need, o.moq) / o.moq) * o.moq * o.unit_price for o in on_time)
            actual = sum(p.total for p in pos)
            m["spend"] += actual
            if actual - cheapest > 0.005:
                m["premium"] += actual - cheapest
                m["premium_parts"] += 1

        # units bought beyond need (supplier minimums, the magnet split)
        for p in res.pos:
            extra = p.quantity - sum(q for _, q in p.serves)
            m["extra_units"] += extra
            m["extra_cost"] += extra * p.unit_price

        # planner workload, split by what requires each hold (same reading as the policy scorecard)
        for p in res.pos:
            m["pos"] += 1
            if not p.rationale.startswith("DRAFT"):
                m["released"] += 1
                continue
            bases = [next((basis for pat, _, basis in REASONS if re.search(pat, r)), None)
                     for r in hold_reasons(p.rationale) if not r.startswith("needs ")]
            m["policy_holds" if any(bases) else "caution_holds"] += 1
    return m


# ---- checks under change ----------------------------------------------------------------------
def check_more_stock(samples):
    """Doubling the stock on hand never makes the agent buy more of a part."""
    cases, fails = 0, []
    for src in samples:
        _, base = plan(copy_of(src, "base_" + src.name))
        path = copy_of(src, "stock_" + src.name)
        edit(path, "UPDATE inventory SET quantity_on_hand = quantity_on_hand * 2 + 10")
        _, more = plan(path)
        before, after = units_by_part(base), units_by_part(more)
        for cid in set(before) | set(after):
            cases += 1
            if after.get(cid, 0) > before.get(cid, 0):
                fails.append(f"{src.stem}: {cid} {before.get(cid, 0)} -> {after.get(cid, 0)} units")
    return cases, fails


def check_more_time(samples):
    """Giving an on-time production order 14 more days never makes it late."""
    cases, fails = 0, []
    for src in samples:
        _, base = plan(copy_of(src, "base_" + src.name))
        for s in base.statuses:
            if not s.on_time:
                continue
            path = copy_of(src, f"time_{s.order.order_id}_{src.name}")
            edit(path, "UPDATE production_schedule SET materials_needed_by = ? WHERE order_id = ?",
                 ((s.order.materials_needed_by + timedelta(days=14)).isoformat(), s.order.order_id))
            _, later = plan(path)
            cases += 1
            if not next(x for x in later.statuses if x.order.order_id == s.order.order_id).on_time:
                fails.append(f"{src.stem}: {s.order.order_id} became late with 14 more days")
    return cases, fails


def check_renamed_ids(samples):
    """Renaming every ID (orders, parts, suppliers, products) never changes a decision."""
    cases, fails = 0, []
    for src in samples:
        _, base = plan(copy_of(src, "base_" + src.name))
        _, renamed = plan(make_variant(src, TMP / ("renamed_" + src.name), None))
        sig = lambda r: sorted((p.quantity, p.unit_price, p.expected_delivery_date, p.rationale.startswith("DRAFT"))
                               for p in r.pos)
        cases += 1
        if sig(base) != sig(renamed) or lateness(base) != lateness(renamed):
            fails.append(f"{src.stem}: decisions differ after renaming IDs")
    return cases, fails


def check_added_supply(samples, arrives_late: bool):
    """Adding an existing order for a part (from an allowed supplier, at catalog price) never makes
    production later. Run twice: the added order arrives the day after today, or 30 days after the
    last need-by date."""
    cases, fails = 0, []
    for src in samples:
        state, base = plan(copy_of(src, "base_" + src.name))
        rules, _ = RULES.base_policy_in_force(state.current_date)
        late0, days0 = lateness(base)
        last_need = max(o.materials_needed_by for o in state.schedule)
        for cid, qty in sorted(units_by_part(base).items()):
            opt = next((o for o in build_options(state, rules, base.tags[cid], cid) if o.eligible and o.mode == "standard"), None)
            if opt is None:
                continue
            arrives = last_need + timedelta(days=30) if arrives_late else state.current_date + timedelta(days=1)
            path = copy_of(src, f"supply_{'late' if arrives_late else 'soon'}_{cid}_{src.name}")
            edit(path, "INSERT INTO purchase_orders (po_number, component_id, supplier_id, quantity, unit_price, "
                       "order_date, expected_delivery_date, rationale) VALUES (?,?,?,?,?,?,?,?)",
                 ("EXT-0001", cid, opt.supplier.supplier_id, qty, opt.unit_price, state.current_date.isoformat(),
                  arrives.isoformat(), "Placed by a planner before this run"))
            _, more = plan(path)
            late1, days1 = lateness(more)
            cases += 1
            if (late1, days1) > (late0, days0):
                fails.append(f"{src.stem}: an existing order for {cid} arriving {arrives} took lateness from "
                             f"{late0} order(s) / {days0} day(s) to {late1} / {days1}")
    return cases, fails


def check_keeps_others_rows(samples):
    """A real agent run never removes alerts it did not write (one written in plain words, one
    written in the agent's own [INFO] format)."""
    cases, fails = 0, []
    mine = ["Call Ostrander Shipyards about the delivery date.", "[INFO] Human-entered follow-up: call customer."]
    for src in samples:
        path = copy_of(src, "rows_" + src.name)
        con = sqlite3.connect(path)
        with con:
            con.executemany("INSERT INTO alerts (description) VALUES (?)", [(t,) for t in mine])
        con.close()
        subprocess.run([sys.executable, str(ROOT / "agent.py"), "--scenario", str(path), "--quiet"],
                       check=True, capture_output=True, text=True)
        con = sqlite3.connect(path)
        left = {r[0] for r in con.execute("SELECT description FROM alerts")}
        con.close()
        for text in mine:
            cases += 1
            if text not in left:
                fails.append(f"{src.stem}: the person's alert \"{text}\" was removed")
    return cases, fails


CHECKS = [
    ("More stock never means more orders", check_more_stock, None),
    ("More time never makes an order late", check_more_time, None),
    ("Renaming IDs never changes a decision", check_renamed_ids, None),
    ("An existing order arriving soon never makes production later",
     lambda s: check_added_supply(s, arrives_late=False), None),
    ("An existing order arriving late never makes production later",
     lambda s: check_added_supply(s, arrives_late=True),
     "README Limitations: late existing orders count as supply"),
    ("A run never removes alerts it did not write", check_keeps_others_rows,
     "README Limitations: the agent recognizes its own alerts by their [INFO] / [ACTION] prefix"),
]


def main() -> int:
    samples = sorted(DATA.glob("scenario_*.sqlite"))
    m = outcome_metrics(samples)
    late_lines = [f"| {scn} | {cust} | {days} | {kind} |" for scn, cust, days, kind in m["late"]]
    avoidable = sum(1 for *_, kind in m["late"] if kind.startswith("AVOIDABLE"))
    lines = [
        "## Outcome metrics on the six samples", "",
        "| Outcome | Metric | Result |", "|---|---|---|",
        f"| Customers on time | Production orders with every part by the need-by date | {m['on_time']} of {m['orders']} |",
        f"| Avoidable lateness | Late orders where an allowed supplier could have made the date, with no rule forcing it | {avoidable} |",
        f"| Cost of the policy | Spend above the cheapest allowed on-time supplier for each part | "
        f"${m['premium']:,.2f} ({m['premium'] / m['spend']:.1%} of ${m['spend']:,.2f}), on {m['premium_parts']} parts |",
        f"| Over-ordering | Units bought beyond need (supplier minimums, magnet split) | "
        f"{m['extra_units']} units, ${m['extra_cost']:,.2f} |",
        f"| Planner workload | Released automatically; held orders the policy requires vs caution only | "
        f"{m['released']} of {m['pos']} ({m['released'] / m['pos']:.0%}); {m['policy_holds']} vs {m['caution_holds']} |",
        "", "Why each late production order is late:", "",
        "| Scenario | Customer | Days late | Why |", "|---|---|---|---|", *late_lines, "",
        "## Checks under change", "",
        "Each check changes the samples in a known way and checks the result moves the right way. No answer key is needed.", "",
        "| Check | Cases | Failed | Known limitation |", "|---|---|---|---|"]
    details = []
    for name, fn, known in CHECKS:
        cases, fails = fn(samples)
        mark = "pass" if not fails else f"**{len(fails)}**"
        lines.append(f"| {name} | {cases} | {mark} | {known or ''} |")
        if fails:
            details += [f"- {name}: {f}" for f in fails[:4]] + ([f"- ... and {len(fails) - 4} more"] if len(fails) > 4 else [])
    if details:
        lines += ["", "Examples of failures:", "", *details]
    report = "\n".join(lines)
    print(report)
    (ROOT / "docs" / "outcomes.md").write_text("# Outcome checks\n\n" + report + "\n")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
