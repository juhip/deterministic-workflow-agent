"""Policy scorecard: grade the agent against the company's own policy, clause by clause.

Black-box by design. For each scenario it copies the database, runs `agent.py` exactly as
a user would, and then judges ONLY what the agent wrote (purchase_orders, alerts) against
the scenario data and the policy numbers in the rulebook. It does not reuse the agent's
planning or supplier-choice code, so a bug there cannot hide itself here.

Not independent of the rulebook: the policy numbers and part labels ("critical", "needs
IEC-62368") come from the same rulebook and classifier the agent uses. So a misreading of the
policy in the rulebook would pass here too; 0 failed checks means compliant with the rulebook's
reading of the policy. The independent checks are the hand-calculated tests and the assumptions
listed for the business to confirm.

Every check names the policy section or memo it enforces and the rubric it belongs to:
  Compliant    hard rules: the order is allowed at all
  Effective    within the rules, it bought well: on time when possible, cheapest unless a rule says otherwise
  Honest       every problem is surfaced, with numbers
  Reliable     reruns are safe and earlier records are untouched
  Generalizes  the same checks pass on scenarios moved to other dates (memo windows open and close)
  Useful       informational: how many holds the policy requires vs. the agent's caution rules

Usage:  python3 evals/policy_scorecard.py            (samples + created scenarios; writes docs/policy_scorecard.md)
        python3 evals/policy_scorecard.py --scenario a.sqlite b.sqlite   (grade any scenario files, e.g. held-out ones)
"""
from __future__ import annotations

import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from procurement.classifier import classify_all          # noqa: E402  (part labels only)
from procurement.db import load_state                    # noqa: E402  (typed read of the scenario)
from procurement.rules import Rulebook                   # noqa: E402  (policy numbers)

from datadir import as_provided, data_dir   # noqa: E402

DATA = data_dir() / "scenarios"


@dataclass
class Check:
    rubric: str
    source: str
    rule: str
    checked: int = 0
    failed: list[str] = field(default_factory=list)       # one line per failed check

    def ok(self, cond: bool, what: str):
        self.checked += 1
        if not cond:
            self.failed.append(what)


CHECKS = {
    "asl":        Check("Compliant", "Policy §2", "Every supplier is on the Approved Supplier List"),
    "certs":      Check("Compliant", "Policy §2.1", "Supplier holds every required certificate (ISO-9001; IEC-62368 for power parts)"),
    "intl":       Check("Compliant", "Policy §3", "International orders state a justification, and it is true"),
    "magnets":    Check("Compliant", "MEMO-2026-018", "Magnets: no supplier above 60%, second supplier at least 25%, or the conflict is escalated"),
    "sole":       Check("Compliant", "Policy §5", "Single-source parts carry a Sole Source Justification"),
    "moq":        Check("Compliant", "Policy §5.1", "Quantity is a whole number at or above the supplier's minimum"),
    "hazmat":     Check("Compliant", "Policy §6", "Hazardous orders are held for procurement review and flagged for Hazmat Storage"),
    "thresholds": Check("Compliant", "Policy §7", "Orders over $40,000 / $120,000 go to the Sourcing Manager / VP"),
    "belowB":     Check("Compliant", "Policy §8", "Suppliers rated below B only when needed, and held for review"),
    "strategic":  Check("Compliant", "Policy §9", "Moving volume from a Strategic supplier that could deliver goes to the VP"),
    "leadtime":   Check("Compliant", "Policy §10", "Delivery date = order date + quoted lead time"),
    "air":        Check("Compliant", "MEMO-2026-044", "Air freight only in its window, international only, Sourcing Manager approves"),
    "pcb":        Check("Compliant", "MEMO-2026-051", "Circuit boards only from previously used suppliers; Certificate of Conformance flagged"),
    "price":      Check("Compliant", "Spec / catalog", "Unit price is the catalog price; order date is the scenario date"),
    "ontime":     Check("Effective", "Policy §10", "No order is late if an allowed supplier could have made the date (unless a rule forced it and it is escalated)"),
    "cheapest":   Check("Effective", "Policy §7", "A cheaper allowed on-time supplier is passed over only with a stated policy reason"),
    "late_alert": Check("Honest", "Spec", "Every late or short production order has an alert with the days late"),
    "options":    Check("Effective", "Spec", "Every late order comes with recovery options checked against the policy"),
    "early":      Check("Honest", "Policy (effective date)", "A scenario dated before the policy took effect is flagged, and the policy still applies"),
    "explained":  Check("Honest", "Spec, Policy §3", "Every order has a rationale and says whether it was released or who must approve"),
    "rerun":      Check("Reliable", "Spec", "A second run buys nothing more"),
    "untouched":  Check("Reliable", "Spec", "Pre-existing purchase orders are unchanged"),
}


# ---- Useful: are holds required by the policy, or only by the agent's caution rules? -------------
# The checks above guard against unsafe releases. This tally guards against the opposite error,
# over-caution: holds the policy does not ask for. It is informational, not pass/fail.
REASONS = [  # (pattern in the agent's hold reason, short name, what requires it; None = a caution rule, not in the policy)
    (r"^moves volume away from Strategic", "moves volume away from a Strategic supplier",
     "Policy §9: VP approval to shift volume from a Strategic supplier"),
    (r"^hazardous material", "hazardous material", "Policy §6: procurement review"),
    (r"^supplier rated .*, below ", "supplier rated below B", "Policy §8: additional review"),
    (r"^air freight", "air freight", "MEMO-2026-044: Sourcing Manager approves each request"),
    (r"over the policy approval threshold", "over a policy approval level", "Policy §7: Sourcing Manager / VP approval"),
    (r"^critical part", "critical part", None),
    (r"^a management memo applies", "a memo covers the part", None),
    (r"^only one supplier is allowed", "only one allowed supplier", None),
    (r"^international supplier", "international supplier", None),
    (r"after a customer need-by date", "arrives after a need-by date", None),
    (r"policy-versus-schedule conflict", "policy-vs-deadline conflict", None),
    (r"auto-release limit", "over the auto-release limit", None),
]
HOLDS = {"orders": 0, "released": 0, "policy": 0, "caution": 0, "reasons": defaultdict(int)}


def hold_reasons(why: str) -> list[str]:
    m = re.search(r"\(because: (.*?)\)\. ", why, re.S)
    return [r.strip() for r in re.split(r";\s+(?=[a-z])", m.group(1))] if m else []


def tally_holds(new: list[dict]) -> None:
    for o in new:
        HOLDS["orders"] += 1
        if not o["why"].startswith("DRAFT"):
            HOLDS["released"] += 1
            continue
        kinds = {next(((name, basis) for p, name, basis in REASONS if re.search(p, r)), ("other (" + r[:40] + ")", None))
                 for r in hold_reasons(o["why"]) if not r.startswith("needs ")}   # the VP note repeats the Strategic reason
        HOLDS["policy" if any(basis for _, basis in kinds) else "caution"] += 1
        for k in kinds:
            HOLDS["reasons"][k] += 1


def holds_report() -> str:
    h, n = HOLDS, max(HOLDS["orders"], 1)
    lines = ["## Useful: how many holds the policy actually requires (informational, not pass/fail)", "",
             "The checks above guard against unsafe releases. This guards against the opposite error, over-caution.", "",
             "| Measure | Orders |", "|---|---|",
             f"| Released automatically | {h['released']} of {h['orders']} ({h['released'] / n:.0%}) |",
             f"| Held, with at least one reason the policy requires a person for | {h['policy']} |",
             f"| Held only for the agent's caution rules (candidates to relax, with evidence) | {h['caution']} |", "",
             "| Hold reason | Held orders citing it | Required by |", "|---|---|---|"]
    for (name, basis), count in sorted(h["reasons"].items(), key=lambda kv: (kv[0][1] is None, -kv[1])):
        lines.append(f"| {name} | {count} | {basis or 'caution rule (not in the policy)'} |")
    return "\n".join(lines)


def rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def run_agent(path: Path):
    subprocess.run([sys.executable, str(ROOT / "agent.py"), "--scenario", str(path), "--quiet"],
                   check=True, capture_output=True, text=True)


def approver(rationale: str) -> str | None:
    m = re.search(r"awaiting approval by (.+?) before release", rationale)
    return m.group(1) if m else None


def ranker(rules: Rulebook, today):
    """Approvers lowest to highest authority, read from the rulebook (default, then each value level)."""
    order = [rules.raw["settings"]["default_approver"]]
    ap = rules.one("approval_thresholds", today)
    for lvl in sorted(ap.params["approval_levels"], key=lambda l: l["above"]) if ap else []:
        if lvl["approver"] not in order:
            order.append(lvl["approver"])
    return lambda who: order.index(who) if who in order else -1


def grade(src: Path, label: str, rules: Rulebook, move_to: str | None = None) -> None:
    work = Path(tempfile.mkdtemp()) / src.name
    shutil.copy(src, work)
    as_provided(work)
    if move_to:
        con = sqlite3.connect(work)
        con.execute('UPDATE scenario_config SET "current_date" = ?', (move_to,))
        con.commit(); con.close()

    st = load_state(work)                           # the world BEFORE the agent acts
    today = st.current_date
    rules, early_note = rules.base_policy_in_force(today)   # same reading of an early-dated scenario as the agent
    before = rows(work, "SELECT * FROM purchase_orders ORDER BY po_number")
    run_agent(work)
    new = [dict(zip(["po", "comp", "sup", "qty", "price", "ordered", "expected", "why"], r)) for r in
           rows(work, "SELECT po_number, component_id, supplier_id, quantity, unit_price, order_date, "
                      "expected_delivery_date, rationale FROM purchase_orders ORDER BY po_number")
           if r[0] not in {b[0] for b in before}]
    alerts = [r[0] for r in rows(work, "SELECT description FROM alerts")]
    after_old = [r for r in rows(work, "SELECT * FROM purchase_orders ORDER BY po_number") if r[0] in {b[0] for b in before}]
    tag = f"{label}: "

    tags = classify_all(st.components, rules, today)
    catalog = {(e.supplier_id, e.component_id): e for e in st.catalog}
    by_comp = defaultdict(list)
    for e in st.catalog:
        by_comp[e.component_id].append(e)
    dom = rules.one("domestic_definition", today)
    countries = {c.lower() for c in dom.params["countries"]} if dom else set()
    domestic = lambda s: s.country.lower() in countries if countries else s.is_domestic
    air_rule = rules.one("expedited_shipping", today)
    prior = {(p.supplier_id, p.component_id) for p in st.existing_pos}
    settings = rules.raw.get("settings", {})
    rank = ranker(rules, today)

    def certs_ok(sid, cid):
        need = set(tags[cid].required_certs)
        have = {c.lower() for c in st.suppliers[sid].certifications}
        return all(n.lower() in have for n in need)

    def freeze_ok(sid, cid):
        for f in rules.for_component("supplier_qualification_freeze", st.components[cid], today):
            ev = f.params["evidence_of_prior_receipt"]
            ok = (ev.get("prior_purchase_order") and (sid, cid) in prior) or \
                 st.suppliers[sid].relationship_tier in ev.get("relationship_tier_in", [])
            if not ok:
                return False
        return True

    def allowed(sid, cid):
        return st.suppliers[sid].on_approved_list and certs_ok(sid, cid) and freeze_ok(sid, cid)

    def leads(e):
        """Possible lead times for a catalog entry: standard, plus air freight when the memo allows it."""
        out = [e.lead_time_days]
        if air_rule and not domestic(st.suppliers[e.supplier_id]):
            p = air_rule.params
            out.append(max(e.lead_time_days - p["lead_time_reduction_days"], p["min_lead_time_days"]))
        return out

    def need_by(o):
        dates = re.findall(r"for its (\d{4}-\d\d-\d\d) need-by", o["why"])
        return min((date.fromisoformat(d) for d in dates), default=None)

    def mentions(name):
        return [a for a in alerts if name in a]

    # ---------------------------------------------------------------- per order
    for o in new:
        s, c, why = st.suppliers[o["sup"]], st.components[o["comp"]], o["why"]
        e = catalog.get((o["sup"], o["comp"]))
        who = approver(why)
        nb = need_by(o)
        ordered, expected = date.fromisoformat(o["ordered"]), date.fromisoformat(o["expected"])
        lead = (expected - ordered).days
        is_air = e is not None and lead != e.lead_time_days
        ident = f"{tag}{o['po']} {c.name} from {s.name}"

        CHECKS["asl"].ok(bool(s.on_approved_list), ident)
        CHECKS["certs"].ok(certs_ok(o["sup"], o["comp"]), ident)
        CHECKS["price"].ok(e is not None and abs(o["price"] - e.unit_price) < 1e-6 and ordered == today, ident)
        CHECKS["moq"].ok(e is not None and float(o["qty"]).is_integer() and o["qty"] >= e.minimum_order_qty, ident)
        CHECKS["leadtime"].ok(e is not None and lead in leads(e), f"{ident}: {lead} days")
        CHECKS["explained"].ok(len(why) > 40 and (why.startswith("RELEASED") or who is not None), ident)
        if is_air:
            CHECKS["air"].ok(air_rule is not None and not domestic(s) and rank(who) >= rank(air_rule.params["approver"]), ident)
        if not domestic(s):
            m = re.search(r"International sourcing justification: (.+?)\(HF-PUR-100 §3([abc])\)", why)
            true = False
            if m:
                on_time_dom = [x for x in by_comp[o["comp"]] if domestic(st.suppliers[x.supplier_id])
                               and allowed(x.supplier_id, o["comp"]) and (nb is None or today + timedelta(min(leads(x))) <= nb)]
                any_dom = [x for x in by_comp[o["comp"]] if domestic(st.suppliers[x.supplier_id]) and allowed(x.supplier_id, o["comp"])]
                prem = rules.one("domestic_preference", today).params
                thr = prem["premium_threshold_critical"] if tags[o["comp"]].critical else prem["premium_threshold"]
                true = {"a": not on_time_dom,
                        # premium is measured against the domestic options that can deliver, or any if none can
                        "b": bool(any_dom) and min(x.unit_price for x in (on_time_dom or any_dom)) > (1 + thr) * o["price"],
                        "c": not any_dom}[m.group(2)]
            CHECKS["intl"].ok(true, ident)
        if len(by_comp[o["comp"]]) == 1:
            CHECKS["sole"].ok("Sole Source" in why or "SOLE SOURCE" in why, ident)
        if c.is_hazardous:
            CHECKS["hazmat"].ok(who is not None and "HAZMAT" in why, ident)
        ap = rules.one("approval_thresholds", today)
        total = o["qty"] * o["price"]
        passed = [l for l in (ap.params["approval_levels"] if ap else []) if total > l["above"]]
        if passed:
            need = max(passed, key=lambda l: l["above"])["approver"]
            CHECKS["thresholds"].ok(rank(who) >= rank(need), ident)
        sus = rules.one("sustainability_preference", today)
        if sus and rules.rating_rank(s.sustainability_rating) < rules.rating_rank(sus.params["last_resort_below_rating"]):
            better = [x for x in by_comp[o["comp"]] if x.supplier_id != o["sup"] and allowed(x.supplier_id, o["comp"])
                      and rules.rating_rank(st.suppliers[x.supplier_id].sustainability_rating)
                      >= rules.rating_rank(sus.params["last_resort_below_rating"])
                      and (nb is None or today + timedelta(min(leads(x))) <= nb)]
            forced = "concentration rule" in why or "MEMO-2026-018" in why
            CHECKS["belowB"].ok(who is not None and (not better or forced), ident)
        strat = rules.one("strategic_preference", today)
        if strat and strat.params.get("shift_away_approver") and s.relationship_tier != strat.params["tier"] \
                and total > settings.get("strategic_shift_significant_above", 0):
            could = [x for x in by_comp[o["comp"]] if st.suppliers[x.supplier_id].relationship_tier == strat.params["tier"]
                     and allowed(x.supplier_id, o["comp"]) and (nb is None or today + timedelta(min(leads(x))) <= nb)]
            if could:
                CHECKS["strategic"].ok(rank(who) >= rank(strat.params["shift_away_approver"]), ident)
        if rules.for_component("supplier_qualification_freeze", c, today):
            CHECKS["pcb"].ok(freeze_ok(o["sup"], o["comp"]) and any("Certificate of Conformance" in a or "Certificate of Conformance" in why
                                                                     for a in alerts + [why]), ident)
        # cheaper allowed on-time supplier skipped: a policy reason must be stated
        cheaper = [x for x in by_comp[o["comp"]] if x.unit_price < o["price"] - 1e-9 and allowed(x.supplier_id, o["comp"])
                   and (nb is None or today + timedelta(min(leads(x))) <= nb)]
        if cheaper:
            reasons = ("domestic", "Strategic", "sustainability", "concentration", "MEMO-2026-018", "B-or-better",
                       "same supplier", "minimum order", "split")
            CHECKS["cheapest"].ok(any(r in why for r in reasons), f"{ident} (cheaper: {', '.join(x.supplier_id for x in cheaper)})")

    tally_holds(new)

    # ------------------------------------------------------- magnets (MEMO-2026-018)
    for cid, comp in st.components.items():
        for r in rules.for_component("concentration_limit", comp, today):
            if not r.params.get("applies_per_order"):
                continue
            mine = [o for o in new if o["comp"] == cid]
            if not mine:
                continue
            vol = defaultdict(float)
            for p in st.existing_pos:
                if p.component_id == cid:
                    vol[p.supplier_id] += p.quantity
            for o in mine:
                vol[o["sup"]] += o["qty"]
            top = max(vol.values()) / sum(vol.values())
            new_vol = defaultdict(float)
            for o in mine:
                new_vol[o["sup"]] += o["qty"]
            second = sorted(new_vol.values())[-2] / sum(new_vol.values()) if len(new_vol) > 1 else 0.0
            ok = top <= r.params["max_share"] + 1e-9 and second >= r.params["min_secondary_share"] - 1e-9
            CHECKS["magnets"].ok(ok or bool(mentions(comp.name)), f"{tag}{comp.name}: top share {top:.0%}")

    # -------------------------------------- production outcome: independent replay
    demand = defaultdict(list)                      # comp -> [(need_by, order_id, qty)]
    for po_ in sorted(st.schedule, key=lambda x: (x.materials_needed_by, x.order_id)):
        for cid, per in st.bom.get(po_.product_id, {}).items():
            demand[cid].append((po_.materials_needed_by, po_.order_id, per * po_.quantity))
    arrivals = defaultdict(list)
    for p in st.existing_pos:
        arrivals[p.component_id].append((p.expected_delivery_date, p.quantity))
    for o in new:
        arrivals[o["comp"]].append((date.fromisoformat(o["expected"]), o["qty"]))
    late = defaultdict(list)                       # order_id -> [(comp, days or None)]
    for cid, ds in demand.items():
        on_hand = st.inventory.get(cid, 0)
        cum = 0.0
        arr = sorted(arrivals[cid])
        for nb, oid, q in ds:
            cum += q
            have = on_hand + sum(qq for d, qq in arr if d <= nb)
            if have + 1e-9 < math.ceil(cum - 1e-9):
                done = next((d for d in sorted({d for d, _ in arr})
                             if on_hand + sum(qq for dd, qq in arr if dd <= d) + 1e-9 >= math.ceil(cum - 1e-9)), None)
                late[oid].append((cid, (done - nb).days if done else None, nb))
    for oid, items in late.items():
        CHECKS["late_alert"].ok(any(oid in a and ("day(s) late" in a or "cannot be fully supplied" in a) for a in alerts),
                                f"{tag}{oid}")
        CHECKS["options"].ok(any(a.startswith(f"[ACTION] Recovery options for {oid} ") and "(A) " in a for a in alerts),
                             f"{tag}{oid}")
        for cid, days, nb in items:
            possible = [x for x in by_comp[cid] if allowed(x.supplier_id, cid) and today + timedelta(min(leads(x))) <= nb]
            name = st.components[cid].name
            escalated = any(a.startswith("[ACTION] Decision needed for " + name) for a in alerts) or \
                        any(name in a and "expedit" in a for a in alerts)
            CHECKS["ontime"].ok(not possible or escalated, f"{tag}{oid} {name}: {days} day(s) late")

    if early_note:
        CHECKS["early"].ok(any("took effect" in a and "applied the current one" in a for a in alerts), f"{tag}not flagged")

    # ---------------------------------------------------------------- reliability
    CHECKS["untouched"].ok(after_old == before, f"{tag}pre-existing purchase orders changed")
    count = len(rows(work, "SELECT po_number FROM purchase_orders"))
    run_agent(work)
    CHECKS["rerun"].ok(len(rows(work, "SELECT po_number FROM purchase_orders")) == count, f"{tag}second run added orders")


SCENARIOS = sorted(DATA.glob("scenario_*.sqlite"))
# The same scenarios moved through time, so memo windows open and close (Generalizes).
MOVED = [("scenario_01_baseline.sqlite", "2026-03-02", "before any memo"),
         ("scenario_01_baseline.sqlite", "2026-08-17", "air-freight window open, before the circuit-board freeze"),
         ("scenario_04_low_inventory.sqlite", "2026-10-28", "air freight in its last days, freeze active")]


def grade_only(paths: list[str]) -> int:
    """Grade the given scenario files (e.g. held-out ones): each is copied, run and checked."""
    rules = Rulebook.load()
    for path in paths:
        grade(Path(path), Path(path).stem, rules)
    lines = ["| Rubric | Source | Rule | Checks | Failed |", "|---|---|---|---|---|"]
    lines += [f"| {c.rubric} | {c.source} | {c.rule} | {c.checked if c.checked else 'not triggered'} | {len(c.failed)} |"
              for c in CHECKS.values()]
    failed_checks = [f for c in CHECKS.values() for f in c.failed]
    print(f"Graded {len(paths)} scenario(s); the files themselves were not changed.\n")
    print("\n".join(lines) + ("\n\nFailed checks:\n" + "\n".join(f"- {f}" for f in failed_checks)
                              if failed_checks else "\n\nNo checks failed."))
    print("\n" + holds_report())
    return 1 if failed_checks else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Grade the agent against the policy, clause by clause.")
    ap.add_argument("--scenario", nargs="+", metavar="FILE",
                    help="grade these scenario files instead of the samples and created scenarios")
    args = ap.parse_args(argv)
    if args.scenario:
        return grade_only(args.scenario)
    import tempfile as _tf
    from gen_scenarios import make_all, make_structural
    rules = Rulebook.load()
    for src in SCENARIOS:
        grade(src, src.stem, rules)
    base = {k: (c.checked, list(c.failed)) for k, c in CHECKS.items()}
    useful = holds_report()                     # the six samples only, before created scenarios add to the tally

    def graded(fn):
        before = {k: (c.checked, len(c.failed)) for k, c in CHECKS.items()}
        fn()
        return (sum(c.checked - before[k][0] for k, c in CHECKS.items()),
                sum(len(c.failed) - before[k][1] for k, c in CHECKS.items()))

    moved = [graded(lambda n=name, d=day: grade(DATA / n, f"{n[:11]}@{d}", rules, move_to=d)) for name, day, _ in MOVED]
    variants = make_all(Path(_tf.mkdtemp()) / "generated")
    renamed = [graded(lambda p=path: grade(p, p.stem, rules)) for path, _ in variants]
    structural = make_structural(Path(_tf.mkdtemp()) / "structural")
    changed = [graded(lambda p=path: grade(p, p.stem, rules)) for path, _ in structural]
    lines = ["| Rubric | Source | Rule | Checks on the 6 scenarios | Failed |", "|---|---|---|---|---|"]
    for k, c in CHECKS.items():
        n, f = base[k]
        lines.append(f"| {c.rubric} | {c.source} | {c.rule} | {n if n else 'not triggered'} | {len(f)} |")
    lines.append(f"| Generalizes | Spec | All checks above, on {len(MOVED)} samples moved to other dates "
                 f"({'; '.join(w for _, _, w in MOVED)}) | {sum(c for c, _ in moved)} | {sum(f for _, f in moved)} |")
    lines.append(f"| Generalizes | Spec | All checks above, on {len(variants)} generated scenarios with new ID formats "
                 f"(JOB-, ITM-, VN-) and new dates ({'; '.join(w for _, w in variants)}) | "
                 f"{sum(c for c, _ in renamed)} | {sum(f for _, f in renamed)} |")
    lines.append(f"| Generalizes | Spec | All checks above, on {len(structural)} scenarios with a changed structure "
                 f"({'; '.join(w for _, w in structural)}) | {sum(c for c, _ in changed)} | {sum(f for _, f in changed)} |")
    failed_checks = [f for c in CHECKS.values() for f in c.failed]
    report = "\n".join(lines) + ("\n\nFailed checks:\n" + "\n".join(f"- {f}" for f in failed_checks) if failed_checks else "\n\nNo checks failed.")
    report += "\n\n" + useful
    print(report)
    (ROOT / "docs" / "policy_scorecard.md").write_text("# Policy scorecard\n\n" + report + "\n")
    return 1 if failed_checks else 0


if __name__ == "__main__":
    sys.exit(main())
