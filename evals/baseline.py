"""Baseline: what does following the policy cost, compared with a naive planner?

The naive planner uses the same shortage calculation as the agent (stock and open orders counted
once, earliest deadline first) but ignores every policy rule: for each part it buys from the
cheapest supplier that arrives in time (MOQ applied), or the fastest if none does, ordering either
once for everything or once per need-by date, whichever is cheaper.

The gap between the two is the cost of compliance, in dollars and in late production orders,
plus the number of naive orders that would break a hard rule. Runs on copies of the samples.

Usage:  python3 evals/baseline.py        (prints a table, writes docs/baseline.md)
"""
from __future__ import annotations

import math
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from procurement.classifier import classify_all          # noqa: E402
from procurement.db import load_state                    # noqa: E402
from procurement.decider import PlannedPO                # noqa: E402
from procurement.pipeline import run                     # noqa: E402
from procurement.planner import net_requirements         # noqa: E402
from procurement.rules import Rulebook                   # noqa: E402
from procurement.validator import replay                 # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datadir import as_provided, data_dir   # noqa: E402

DATA = data_dir() / "scenarios"


def _cheapest(state, comp, qty, need_by):
    """Cheapest offer arriving by need_by (MOQ applied), else the fastest. Returns (arrive, cost, entry, units, late)."""
    today = state.current_date
    offers = [(today + timedelta(days=e.lead_time_days), max(qty, e.minimum_order_qty) * e.unit_price, e,
               max(qty, e.minimum_order_qty)) for e in state.catalog_for(comp)]
    if not offers:
        return None
    on_time = [o for o in offers if o[0] <= need_by]
    best = min(on_time, key=lambda o: o[1]) if on_time else min(offers, key=lambda o: (o[0], o[1]))
    return (*best, not on_time)


def naive_plan(state) -> list[PlannedPO]:
    """Per part, the cheaper of: one order for everything by the first need-by date, or one order per
    need-by date; each from the cheapest supplier that arrives in time. Ignores the policy entirely."""
    today = state.current_date
    pos = []
    for comp, req in sorted(net_requirements(state).items()):
        if not req.tranches:
            continue
        one = _cheapest(state, comp, req.to_buy, min(t.need_by for t in req.tranches))
        each = [(_cheapest(state, comp, t.qty, t.need_by), t) for t in req.tranches]
        if one is None:
            continue
        split_ok = all(c for c, _ in each) and sum(c[4] for c, _ in each) <= one[4] * len(each)
        if split_ok and sum(c[1] for c, _ in each) < one[1]:
            picks = [(c, t.orders) for c, t in each]
        else:
            picks = [(one, [oid for t in req.tranches for oid in t.orders])]
        for (arrive, _, e, units, _), serves in picks:
            pos.append(PlannedPO(comp, e.supplier_id, int(math.ceil(units)), e.unit_price, today, arrive, "standard", serves))
    return pos


def hard_rule_breaks(state, rules: Rulebook, pos: list[PlannedPO]) -> list[str]:
    """Which naive orders the policy would forbid: approved list, certificates, magnet split, circuit-board freeze."""
    today = state.current_date
    tags = classify_all(state.components, rules, today)
    out, seen = [], set()
    for p in pos:
        sup, comp = state.suppliers[p.supplier_id], state.components[p.component_id]
        if not sup.on_approved_list:
            out.append(f"{comp.name}: {sup.name} is not on the approved list")
        missing = [c for c in tags[p.component_id].required_certs if c not in sup.certifications]
        if missing:
            out.append(f"{comp.name}: {sup.name} lacks {', '.join(missing)}")
        for r in rules.for_component("concentration_limit", comp, today):
            if r.params.get("applies_per_order") and (comp.component_id, r.id) not in seen:
                seen.add((comp.component_id, r.id))                       # judged once per part, over all its orders
                vol: dict[str, int] = {}
                for q in pos:
                    if q.component_id == p.component_id:
                        vol[q.supplier_id] = vol.get(q.supplier_id, 0) + q.quantity
                top = max(vol.values()) / sum(vol.values())
                if top > r.params.get("max_share", 1):
                    out.append(f"{comp.name}: one supplier takes {top:.0%} ({r.citation} caps it at {r.params['max_share']:.0%})")
        for f in rules.for_component("supplier_qualification_freeze", comp, today):
            ev = f.params.get("evidence_of_prior_receipt", {})
            prior = any(x.component_id == p.component_id and x.supplier_id == p.supplier_id for x in state.existing_pos)
            if not (prior and ev.get("prior_purchase_order")) and sup.relationship_tier not in ev.get("relationship_tier_in", []):
                out.append(f"{comp.name}: {sup.name} has no prior receipt ({f.citation})")
    return out


def late(statuses) -> int:
    return sum(1 for s in statuses if not s.on_time)


def main() -> int:
    rules = Rulebook.load()
    lines = ["| Scenario | Agent spend | Naive spend | Cost of compliance | Late orders (agent / naive) | Hard-rule breaks in the naive plan |",
             "|---|---|---|---|---|---|"]
    tot = [0.0, 0.0]
    examples = []
    for src in sorted(DATA.glob("scenario_*.sqlite")):
        work = Path(tempfile.mkdtemp()) / src.name            # always a copy
        shutil.copy(src, work)
        as_provided(work)
        state = load_state(work)
        agent = run(state, rules)
        naive = naive_plan(state)
        a_spend, n_spend = sum(p.total for p in agent.pos), sum(p.total for p in naive)
        breaks = hard_rule_breaks(state, rules, naive)
        examples += [f"{src.stem}: {b}" for b in dict.fromkeys(breaks)]
        tot[0] += a_spend; tot[1] += n_spend
        lines.append(f"| {src.stem.replace('scenario_', '')} | ${a_spend:,.2f} | ${n_spend:,.2f} | "
                     f"${a_spend - n_spend:+,.2f} | {late(agent.statuses)} / {late(replay(state, naive, {}))} | {len(breaks)} |")
    lines.append(f"| **Total** | ${tot[0]:,.2f} | ${tot[1]:,.2f} | ${tot[0] - tot[1]:+,.2f} | | |")
    report = "\n".join(lines) + "\n\nExamples of naive orders the policy forbids:\n" + "\n".join(f"- {e}" for e in examples)
    print(report)
    (ROOT / "docs" / "baseline.md").write_text("# Baseline: cost of compliance\n\n" + report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
