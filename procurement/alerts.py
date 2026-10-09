"""Layer 5c: alerts - plain-English problems and recommendations for people.

Severity tells the reader what to do:
  CRITICAL  production is at risk or an order could not be placed
  WARNING   a policy rule could not be fully met, or the data looks wrong
  ACTION    a named person must approve or review something
  INFO      context: assumptions made, documents that conflict, run summary
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .classifier import ComponentTags
from .decider import ComponentDecision, PlannedPO
from .models import ScenarioState
from .release import approval_levels, conflict_decider
from .rules import Rulebook
from .validator import OrderStatus

ORDER = {"CRITICAL": 0, "WARNING": 1, "ACTION": 2, "INFO": 3}


@dataclass(frozen=True)
class Alert:
    severity: str
    text: str

    def render(self) -> str:
        return f"[{self.severity}] {self.text}"


def air_freight_cost(po: PlannedPO, state: ScenarioState, rules: Rulebook) -> str:
    """The air-freight memo prices freight per kg. Parts bought by the kg can be estimated;
    parts bought by the piece cannot, because the data has no unit weights."""
    air = rules.one("expedited_shipping", state.current_date)
    lo, hi = air.params.get("cost_per_kg_range", [None, None]) if air else (None, None)
    if lo is not None and state.components[po.component_id].unit_of_measure.lower() == "kg":
        return (f"AIR FREIGHT COST - about ${po.quantity * lo:,.0f}-${po.quantity * hi:,.0f} ({po.quantity:g} kg at "
                f"${lo}-${hi}/kg, {air.citation}); document it on the order")
    return "AIR FREIGHT COST - not computable: the data has no unit weights for this part; the business to supply the freight quote"


def recipient(rules: Rulebook, key: str) -> str:
    """Who an alert is addressed to (a rulebook setting, so the company's own team names are used)."""
    return rules.raw.get("settings", {})[key]

def po_flags(po: PlannedPO, dec: ComponentDecision, state: ScenarioState, rules: Rulebook,
             tags: ComponentTags, urgent: bool) -> list[str]:
    """Things a human must know about one purchase order. Used in its rationale and in alerts."""
    today = state.current_date
    sup = state.suppliers[po.supplier_id]
    flags: list[str] = []
    if tags.hazardous:
        hz = rules.one("hazmat_handling", today)
        flags.append(f"HAZMAT: flag for procurement review; {recipient(rules, 'receiving_team')}: receive into Hazmat Storage, "
                     f"FIFO ({hz.citation if hz else 'policy'})")
    opt = next(o for o in dec.options if o.supplier.supplier_id == po.supplier_id and o.mode == po.mode)
    flags.extend(f"PENDING APPROVAL - {f}" for f in opt.flags)
    if po.mode == "air":
        flags.append(air_freight_cost(po, state, rules))
    ap = rules.one("approval_thresholds", today)
    levels = approval_levels(rules, today)
    passed = [(above, who) for above, who in levels if po.total > above]
    if ap and passed:
        above, who = passed[-1]
        p = ap.params
        if urgent and len(passed) == 1 and po.total <= p.get("emergency_bypass_up_to", 0):
            flags.append(f"EMERGENCY ORDER - ${po.total:,.0f} placed to prevent a stoppage; retroactive "
                         f"{p['emergency_retroactive_approver']} approval due within {p['retroactive_business_days']} "
                         f"business days ({ap.citation})")
        else:
            flags.append(f"PENDING APPROVAL - ${po.total:,.0f} exceeds ${above:,}: {who} ({ap.citation})")
    sus = rules.one("sustainability_preference", today)
    if sus and rules.rating_rank(sup.sustainability_rating) < rules.rating_rank(sus.params["last_resort_below_rating"]):
        flags.append(f"REVIEW - supplier rated {sup.sustainability_rating}, below {sus.params['last_resort_below_rating']}: "
                     f"additional review required ({sus.citation})")
    for doc in rules.for_component("shipment_documentation", state.components[po.component_id], today):
        flags.append(f"RECEIVING - {doc.params['requirement']}; reject shipments without it ({doc.citation})")
    if len({e.supplier_id for e in state.catalog_for(po.component_id)}) == 1:
        base = next((r for r in rules.active("concentration_limit", today) if not r.overrides), None)
        flags.append("SOLE SOURCE - only supplier for this part in the catalog; Sole Source Justification required"
                     + (f" ({base.citation})" if base else ""))
    return flags


def assumptions_used(state: ScenarioState, rules: Rulebook, tags: dict[str, ComponentTags], pos: list[PlannedPO],
                     flags: dict[int, list[str]], releases: list | None) -> list[str]:
    """Only the gap-filling assumptions that actually shaped THIS run (others are flagged where they arise)."""
    today, out = state.current_date, []
    if not pos:
        return out
    bought = {p.component_id for p in pos}
    shared = {c for c in bought if sum(1 for o in state.schedule if c in state.bom.get(o.product_id, {})) > 1}
    if shared:
        out.append("When production orders compete for the same parts, stock goes to the earliest need-by date first, "
                   "then by order number; no customer priority was given.")
    hz = rules.one("hazmat_handling", today)
    docs = [d for c in bought for d in rules.for_component("shipment_documentation", state.components[c], today)]
    if any(tags[c].hazardous for c in bought) or docs:
        cites = ", ".join(sorted({r.citation for r in ([hz] if hz else []) + docs}))
        out.append(f"No extra days were added for hazardous-material receiving or incoming inspection ({cites}); "
                   "the documents give no number.")
    air = rules.one("expedited_shipping", today)
    if any(p.mode == "air" for p in pos) and air:
        out.append(f"Every scheduled order is treated as a confirmed production order for air freight ({air.citation}); "
                   "the schedule has no status column.")
    strat = rules.one("strategic_preference", today)
    if strat and releases and any(r.startswith("moves volume away from") for rel in releases for r in rel.reasons):
        floor = rules.raw.get("settings", {}).get("strategic_shift_significant_above", 0)
        size = f"Any order above ${floor:,}" if floor else "Any order, whatever its size,"
        out.append(f"{size} that moves volume away from a Strategic supplier counts as 'significant' "
                   f"({strat.citation}); the policy does not define the term.")
    ap = rules.one("approval_thresholds", today)
    if ap and any(f.startswith("EMERGENCY") for fl in flags.values() for f in fl):
        out.append(f"Emergency orders are still held for approval, the cautious reading of {ap.citation}, which allows "
                   "them to bypass approval.")
    out.append("Supplier capacity is treated as unlimited and prices as unit prices only: the data has no capacity, "
               f"shipping or handling figures{f' ({ap.citation} asks for total cost of ownership)' if ap else ''}.")
    return out


def build_alerts(state: ScenarioState, rules: Rulebook, tags: dict[str, ComponentTags],
                 decisions: dict[str, ComponentDecision], pos: list[PlannedPO], flags: dict[int, list[str]],
                 statuses: list[OrderStatus], violations: list[tuple[PlannedPO, str]],
                 releases: list | None = None) -> list[Alert]:
    today = state.current_date
    comps = state.components
    alerts: list[Alert] = []
    add = lambda sev, text: alerts.append(Alert(sev, text))

    # ---- data and schedule ---------------------------------------------------------------
    for issue in state.issues:
        add("WARNING", f"Data problem: {issue}")
    for o in state.schedule:
        if o.materials_needed_by < today:
            add("WARNING", f"{o.order_id} ({o.customer}) needed materials by {o.materials_needed_by}, before today ({today}). "
                           "It is already late; purchases for it are planned as urgent.")

    # ---- could not buy -------------------------------------------------------------------
    for comp_id, dec in decisions.items():
        if dec.no_eligible_supplier:
            why = "; ".join(f"{o.supplier.name} ({o.supplier.supplier_id}): {', '.join(o.excluded_because)}"
                            for o in dec.options if o.mode == "standard") or "no supplier in the catalog sells it"
            orders = ", ".join(sorted({oid for t in dec.requirement.tranches for oid, _ in t.orders}))
            add("CRITICAL", f"Cannot buy {dec.requirement.to_buy} x {comps[comp_id].name} ({comp_id}) for {orders}: no supplier "
                            f"is allowed under the policy ({why}). No order placed; escalate to the {conflict_decider(rules)}.")
    for po, reason in violations:
        add("CRITICAL", f"Blocked a planned order for {comps[po.component_id].name} from {po.supplier_id}: {reason}. "
                        "It was not written; this indicates an agent defect and should be reviewed.")

    # ---- late production orders ----------------------------------------------------------
    for st in statuses:
        if st.on_time:
            continue
        o = st.order
        head = f"Cannot meet {o.materials_needed_by} start date for {o.order_id} ({o.customer}, {o.quantity} x {o.product_id})"
        price = (state.products.get(o.product_id) or {}).get("unit_price")
        short_units = o.quantity - st.buildable_on_time
        at_risk = (f" Revenue at risk: {short_units} x {state.products[o.product_id].get('name', o.product_id)} at "
                   f"${price:,.0f} = ${short_units * price:,.0f}." if price and short_units > 0 else "")
        if st.ready is None:
            parts = ", ".join(f"{comps[c].name} ({c})" for c, _ in st.limiting)
            add("CRITICAL", f"{head}: {parts} cannot be fully supplied. {st.buildable_on_time} of {o.quantity} units can be built."
                            + at_risk)
            continue
        causes = []
        for c, r in st.limiting:
            dec = decisions.get(c)
            new_arrivals = {p.expected_delivery_date for p in pos if p.component_id == c}
            if not any(d <= r for d in new_arrivals):
                causes.append(f"{comps[c].name} ({c}) is covered by an order already placed that arrives {r}; the agent "
                              "does not buy duplicate stock, so consider expediting that order")
            elif dec and dec.alternative and "Protecting the schedule" in dec.alternative:
                causes.append(f"{comps[c].name} ({c}) arrives {r} because the agent followed "
                              f"{dec.concentration.citation if dec.concentration else 'policy'} (see decision alert)")
            else:
                causes.append(f"{comps[c].name} ({c}) arrives {r}; no allowed supplier can deliver sooner")
        add("CRITICAL", f"{head}: {'; '.join(causes)}. Materials complete {st.ready}, {st.late_days} day(s) late. "
                        f"{st.buildable_on_time} of {o.quantity} units can be built by the need-by date.{at_risk} "
                        f"Recommend telling {o.customer} and confirming a revised start date.")

    # ---- policy could not be fully met -----------------------------------------------------
    for comp_id, dec in decisions.items():
        for c in dec.conflicts:
            add("WARNING", f"{comps[comp_id].name} ({comp_id}): {c}")
        if dec.alternative:
            add("ACTION", f"Decision needed for {comps[comp_id].name} ({comp_id}): the plan follows "
                          f"{dec.concentration.citation if dec.concentration else 'the policy'}. {dec.alternative} "
                          f"The {conflict_decider(rules)} should confirm or grant an exception.")
        allowed = {o.supplier.supplier_id for o in dec.eligible}
        if dec.requirement.tranches and len(allowed) == 1 and not dec.no_eligible_supplier:
            sid = next(iter(allowed))
            blocked = sorted({o.supplier.supplier_id for o in dec.options if not o.eligible})
            sev = "WARNING" if tags[comp_id].critical else "INFO"
            base = next((r for r in rules.active("concentration_limit", today) if not r.overrides), None)
            extra = (f" Policy requires at least {base.params['min_qualified_suppliers_critical']} qualified suppliers and a "
                     f"{base.params['max_share_critical']:.0%} cap for critical parts ({base.citation}); neither can be met."
                     if tags[comp_id].critical and base else "")
            add(sev, f"Single-supplier risk for {comps[comp_id].name} ({comp_id}): only {state.suppliers[sid].name} ({sid}) is "
                     f"allowed{' (' + ', '.join(blocked) + ' blocked by policy)' if blocked else ''}.{extra}")

    # 12-month concentration limits: report, since history is not available to judge them
    over = []
    for comp_id, dec in decisions.items():
        rule = dec.concentration
        if not rule or rule.params.get("applies_per_order") or len({o.supplier.supplier_id for o in dec.eligible}) < 2:
            continue
        cap = rule.params["max_share_critical" if tags[comp_id].critical else "max_share_noncritical"]
        totals: dict[str, float] = {}
        for p in state.existing_pos:
            if p.component_id == comp_id:
                totals[p.supplier_id] = totals.get(p.supplier_id, 0) + p.quantity
        for p in pos:
            if p.component_id == comp_id:
                totals[p.supplier_id] = totals.get(p.supplier_id, 0) + p.quantity
        if totals:
            sid = max(totals, key=totals.get)
            share = totals[sid] / sum(totals.values())
            if share > cap + 1e-9:
                over.append((tags[comp_id].critical, f"{comps[comp_id].name} {share:.0%} with {sid} (limit {cap:.0%})"))
    if over:
        crit = [t for c, t in over if c]
        months = next((d.concentration.params.get("window_months") for d in decisions.values()
                       if d.concentration and d.concentration.params.get("window_months")), None)
        add("INFO", f"Supplier concentration limits are measured over a rolling {months} months and no purchase history was "
                    "provided, so single orders were not split to meet them. "
                    + (f"Critical parts where this run alone exceeds the limit: {'; '.join(crit)}. " if crit else "")
                    + f"{len(over) - len(crit)} non-critical part(s) are also single-sourced in this run. "
                    f"Provide {months}-month purchase history to check these properly.")

    # ---- approvals and reviews (one alert per flagged order) --------------------------------
    for i, po in enumerate(pos):
        for f in flags.get(i, []):
            # approvals and reviews are listed in the approval queue below; these two are handling notes
            if f.startswith(("EMERGENCY", "HAZMAT")):
                add("ACTION", f"{comps[po.component_id].name} ({po.component_id}) from {state.suppliers[po.supplier_id].name}, "
                              f"{po.quantity} units: {f}.")
    coc = [po for i, po in enumerate(pos) if any(f.startswith("RECEIVING") for f in flags.get(i, []))]
    if coc:
        add("ACTION", f"{recipient(rules, 'receiving_team')}: " + next(f for f in flags[pos.index(coc[0])] if f.startswith("RECEIVING")) +
                      f". Applies to {len(coc)} order(s) placed today.")

    # ---- context: assumptions and document conflicts ----------------------------------------
    seen = set()
    for comp_id in decisions:
        for note in tags[comp_id].alias_notes:
            if note not in seen:
                seen.add(note)
                add("INFO", f"Document mismatch: {note}. Please confirm.")
    for comp_id, dec in decisions.items():
        freeze = rules.for_component("supplier_qualification_freeze", comps[comp_id], today)
        if not freeze or not dec.requirement.tranches:
            continue
        f = freeze[0]
        # suppliers blocked ONLY by the freeze (a banned supplier would be blocked anyway)
        blocked = [o for o in dec.options if o.mode == "standard" and o.excluded_because
                   and all("freeze" in r for r in o.excluded_because)]
        if blocked:
            cheapest = min(blocked, key=lambda o: o.unit_price)
            add("INFO", f"{f.citation} new-supplier freeze: the database has no receiving history, so a supplier counts as "
                        f"'previously received from' if it has a prior order for {comp_id} or a "
                        f"{'/'.join(f.params['evidence_of_prior_receipt']['relationship_tier_in'])} relationship. Blocked: "
                        + ", ".join(f"{o.supplier.name} (${o.unit_price:.2f})" for o in blocked)
                        + f". Cheapest blocked option was {cheapest.supplier.name}. Please confirm the receiving history.")
        est = f.params.get("estimated_duration_days")
        if est and today >= f.effective_from + timedelta(days=est[0]):
            add("INFO", f"{f.citation} expected to last {est[0]}-{est[1]} days from {f.effective_from}; that window has "
                        "started. Confirm with Quality whether the freeze is still in force.")
    dom = rules.one("domestic_definition", today)
    if dom:
        countries = {c.lower() for c in dom.params["countries"]}
        mismatched = [s for s in state.suppliers.values()
                      if s.country and (s.country.lower() in countries) != s.is_domestic]
        if mismatched:
            add("INFO", f"Policy ({dom.citation}) counts {', '.join(dom.params['countries'][-1:])} as domestic, but the "
                        "database marks " + ", ".join(f"{s.name} ({s.country})" for s in mismatched) +
                        " as not domestic. The agent follows the policy; please correct one or the other.")
    ended = [r for r in rules.rules if r.type == "expedited_shipping" and r.effective_to and r.effective_to < today]
    if ended and any(not s.on_time for s in statuses):
        add("INFO", f"Air-freight authorization ({ended[0].citation}) ended {ended[0].effective_to}, so it was not used. "
                    "A new authorization could shorten international lead times for the late orders above.")

    # ---- approval queue: one request per approver, most urgent first -------------------------
    releases = releases or []
    held = [(r, p) for r, p in zip(releases, pos) if r.needs_approval]
    for who in sorted({r.approver for r, _ in held}):
        items = sorted(((r, p) for r, p in held if r.approver == who), key=lambda rp: rp[0].slack_days)
        lines = [f"{comps[p.component_id].name} from {state.suppliers[p.supplier_id].name}, {p.quantity} units, "
                 f"${p.total:,.2f}, arrives {p.expected_delivery_date}"
                 + (f" ({-r.slack_days} day(s) late)" if r.slack_days < 0 else f" ({r.slack_days} day(s) to spare)")
                 + f" - because: {'; '.join(r.reasons)}" for r, p in items]
        add("ACTION", f"Approval needed from {who} for {len(items)} draft order(s) totalling "
                      f"${sum(p.total for _, p in items):,.2f}, most urgent first. Nothing is sent to these suppliers until "
                      f"approved: " + " | ".join(f"({i}) {t}" for i, t in enumerate(lines, 1)))
    auto = [p for r, p in zip(releases, pos) if not r.needs_approval]
    if auto:
        add("INFO", f"{len(auto)} routine order(s) totalling ${sum(p.total for p in auto):,.2f} were released "
                    "automatically: none matched an approval exception (value cap, critical part, memo, international or "
                    "low-rated supplier, air freight, hazmat, single supplier, late arrival, policy conflict).")

    used = assumptions_used(state, rules, tags, pos, flags, releases)
    if used:
        add("INFO", "Assumptions this run relied on, because the policy or the data is silent (please confirm or correct): "
                    + " ".join(f"({i}) {a}" for i, a in enumerate(used, 1)))

    alerts.sort(key=lambda a: ORDER[a.severity])
    late = sum(1 for s in statuses if not s.on_time)
    spend = sum(p.total for p in pos)
    n_held = sum(1 for r in releases if r.needs_approval)
    status = f"{len(pos) - n_held} released automatically and {n_held} awaiting approval" if releases else "placed"
    alerts.append(Alert("INFO", f"Run summary for {today}: {len(pos)} purchase orders ({status}), totalling ${spend:,.2f} across "
                                f"{len({p.component_id for p in pos})} parts; {len(statuses) - late} of {len(statuses)} "
                                f"production orders fully supplied on time, {late} at risk."))
    return alerts
