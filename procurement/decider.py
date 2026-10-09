"""Layer 4: choose suppliers and quantities for each component on the shopping list.

How a supplier is chosen, in order of importance (each step cites the rulebook):
  1. Only suppliers that passed the hard rules (options.py) are considered.
  2. On time beats late.  If nobody can make the date, pick whoever arrives first.
  3. Domestic first. International only if no domestic supplier is on time, the domestic
     price premium is above the threshold (30%, or 45% for critical parts), or nobody
     domestic sells it.
  4. Suppliers rated below B only if no one better is available.
  5. Cheapest, after counting minimum-order surplus, with two policy nudges:
       - a Strategic partner wins if the cheaper option saves 12% or less;
       - among options within 8% on price and ~4 business days on delivery, prefer
         sustainability A or better, then ISO-14001.
  6. One supplier for the whole need when that supplier can meet every date; otherwise
     split by deadline, reusing an earlier supplier when it is within 10% on price.
  7. Order quantities are rounded up to each supplier's minimum order quantity.
  8. Per-order concentration rules (the magnet memo) are then enforced by a small search
     that shifts volume between suppliers and keeps any shift that reduces rule-breaking,
     then lateness, then cost.  Whether the rule or the schedule wins a conflict is a
     setting in the rulebook (default: follow the rule).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .classifier import ComponentTags
from .models import ScenarioState
from .options import SupplierOption, build_options
from .planner import Requirement
from .rules import Rule, Rulebook

EPS = 1e-9


@dataclass
class Line:
    tranche: int
    option: SupplierOption
    qty: int


@dataclass
class PlannedPO:
    component_id: str
    supplier_id: str
    quantity: int
    unit_price: float
    order_date: date
    expected_delivery_date: date
    mode: str
    serves: list[tuple[str, int]] = field(default_factory=list)  # (production order, units)
    rationale: str = ""

    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass
class ComponentDecision:
    component_id: str
    requirement: Requirement
    options: list[SupplierOption]
    lines: list[Line] = field(default_factory=list)
    choice_reasons: dict[tuple, str] = field(default_factory=dict)   # option key -> why chosen
    intl_reasons: dict[tuple, str] = field(default_factory=dict)     # option key -> POL §3 justification
    surplus: dict[tuple, int] = field(default_factory=dict)          # option key -> units over need (MOQ)
    concentration: Rule | None = None
    conflicts: list[str] = field(default_factory=list)               # rule could not be fully met
    alternative: str = ""                                            # what the other priority would do
    no_eligible_supplier: bool = False

    @property
    def eligible(self) -> list[SupplierOption]:
        return [o for o in self.options if o.eligible]


def key(o: SupplierOption) -> tuple[str, str]:
    return (o.supplier.supplier_id, o.mode)


class Decider:
    def __init__(self, state: ScenarioState, rules: Rulebook, tags: dict[str, ComponentTags]):
        self.state, self.rules, self.tags = state, rules, tags
        self.today = state.current_date
        s = rules.raw.get("settings", {})
        self.follow_rule = s.get("when_rule_conflicts_with_schedule", "follow_rule") == "follow_rule"
        self.reuse_pct = s.get("reuse_supplier_if_within_pct", 0.10)
        self.days_per_5bd = s["calendar_days_per_5_business_days"]
        self.max_extra_pct = s.get("max_extra_units_to_meet_split_pct", 0.25)

    # ---- small helpers -------------------------------------------------------------------
    def buffer_days(self, comp_id: str) -> int:
        if not self.tags[comp_id].hazardous:
            return 0
        rule = self.rules.one("hazmat_handling", self.today)
        return int((rule.params.get("receiving_buffer_days") if rule else 0) or 0)

    def ready(self, o: SupplierOption, comp_id: str) -> date:
        return o.arrival + timedelta(days=self.buffer_days(comp_id))

    def on_time(self, o: SupplierOption, need_by: date, comp_id: str) -> bool:
        return self.ready(o, comp_id) <= need_by

    def eff_price(self, o: SupplierOption, qty: int) -> float:
        """Unit price after counting the surplus forced by the minimum order quantity."""
        return o.unit_price * max(qty, o.moq) / max(qty, 1)

    def intl_reason(self, o: SupplierOption, need_by: date, pool: list[SupplierOption], comp_id: str) -> str | None:
        """'' for domestic; a POL §3 justification if international is allowed; None if not allowed."""
        if o.domestic:
            return ""
        rule = self.rules.one("domestic_preference", self.today)
        if rule is None:
            return "no domestic-preference rule in force"
        dom = [d for d in pool if d.domestic]
        if not dom:
            return f"no domestic supplier offers this part ({rule.citation}c)"
        dom_ok = [d for d in dom if self.on_time(d, need_by, comp_id)]
        if not dom_ok:
            return f"no domestic supplier can deliver by {need_by} ({rule.citation}a)"
        thr = rule.params["premium_threshold_critical" if self.tags[comp_id].critical else "premium_threshold"]
        best = min(d.unit_price for d in dom_ok)
        premium = (best - o.unit_price) / o.unit_price
        if premium > thr + EPS:
            return f"domestic price premium {premium:.0%} exceeds the {thr:.0%} threshold ({rule.citation}b)"
        return None

    # ---- ranking ------------------------------------------------------------------------
    def _pick(self, pool: list[SupplierOption], qty: int) -> tuple[SupplierOption, str]:
        price = {key(o): self.eff_price(o, qty) for o in pool}
        cheapest = min(pool, key=lambda o: (price[key(o)], o.lead_days))
        low = price[key(cheapest)]

        strat = self.rules.one("strategic_preference", self.today)
        if strat:
            p = strat.params
            partners = [o for o in pool if o.supplier.relationship_tier == p["tier"]
                        and low >= (1 - p["max_savings_to_stay"]) * price[key(o)] - EPS]
            if partners:
                best = min(partners, key=lambda o: (price[key(o)], o.lead_days))
                if key(best) == key(cheapest):
                    return best, "lowest price among allowed suppliers that can deliver on time"
                saving = 1 - low / price[key(best)]
                return best, (f"{p['tier']} partner; the cheaper {cheapest.supplier.name} would save only {saving:.0%} "
                              f"(stay if <= {p['max_savings_to_stay']:.0%}, {strat.citation})")

        sus = self.rules.one("sustainability_preference", self.today)
        if sus:
            p = sus.params
            lead_window = p["comparable_business_days"] * self.days_per_5bd / 5
            group = [o for o in pool if price[key(o)] <= (1 + p["comparable_price_pct"]) * low + EPS
                     and abs(o.lead_days - cheapest.lead_days) <= lead_window]
            good = self.rules.rating_rank(p["preferred_min_rating"])
            iso = p.get("preferred_certification", "").lower()
            best = min(group, key=lambda o: (-(self.rules.rating_rank(o.supplier.sustainability_rating) >= good),
                                             -(iso in [c.lower() for c in o.supplier.certifications]),
                                             price[key(o)], o.lead_days, o.mode == "air"))
            if key(best) != key(cheapest):
                return best, (f"sustainability preference: rated {best.supplier.sustainability_rating} and within "
                              f"{p['comparable_price_pct']:.0%} of the cheapest price ({sus.citation})")
        return cheapest, "lowest price among allowed suppliers that can deliver on time"

    def rank(self, eligible: list[SupplierOption], need_by: date, qty: int, comp_id: str) -> list[tuple[SupplierOption, str]]:
        """Every eligible option, best first, each with a one-line reason."""
        on = [o for o in eligible if self.on_time(o, need_by, comp_id)]
        # air freight only counts when the same supplier's normal shipping would be late
        on = [o for o in on if not (o.mode == "air" and any(
            x.mode == "standard" and x.supplier.supplier_id == o.supplier.supplier_id for x in on))]
        allowed = [o for o in on if self.intl_reason(o, need_by, eligible, comp_id) is not None]

        sus = self.rules.one("sustainability_preference", self.today)
        floor = self.rules.rating_rank(sus.params["last_resort_below_rating"]) if sus else 0
        ordered: list[tuple[SupplierOption, str]] = []
        good = [o for o in allowed if self.rules.rating_rank(o.supplier.sustainability_rating) >= floor]
        poor = [o for o in allowed if o not in good]
        for group, tag in ((good, ""), (poor, " (rated below B: used only because no better-rated supplier fits)")):
            rest = list(group)
            while rest:
                o, why = self._pick(rest, qty)
                cheaper_poor = [p for p in poor if p is not o and self.eff_price(p, qty) < self.eff_price(o, qty)]
                if not tag and cheaper_poor:
                    c = min(cheaper_poor, key=lambda p: self.eff_price(p, qty))
                    why = (f"{why.replace('lowest price among allowed', 'lowest price among B-or-better')}; the cheaper "
                           f"{c.supplier.name} is rated {c.supplier.sustainability_rating}, below B, so it is a last resort "
                           f"({sus.citation if sus else 'policy'})")
                ordered.append((o, why + tag))
                rest.remove(o)
        late = sorted((o for o in eligible if not self.on_time(o, need_by, comp_id)),
                      key=lambda o: (self.ready(o, comp_id), not o.domestic, self.eff_price(o, qty)))
        for o in late:
            ordered.append((o, f"no allowed supplier can deliver by {need_by}; this is the earliest arrival "
                               f"({self.ready(o, comp_id)})"))
        return ordered

    # ---- main entry ---------------------------------------------------------------------
    def decide(self, req: Requirement) -> ComponentDecision:
        comp_id = req.component_id
        options = build_options(self.state, self.rules, self.tags[comp_id], comp_id)
        dec = ComponentDecision(comp_id, req, options)
        comp = self.state.components[comp_id]
        rules = self.rules.for_component("concentration_limit", comp, self.today)
        dec.concentration = rules[0] if rules else None
        if not req.tranches:
            return dec
        eligible = dec.eligible
        if not eligible:
            dec.no_eligible_supplier = True
            return dec

        tr = req.tranches
        split_lines, split_reasons = self._split_by_deadline(req, eligible)
        top, why = self.rank(eligible, tr[0].need_by, req.to_buy, comp_id)[0]
        dec.lines, dec.choice_reasons = split_lines, split_reasons
        if self.on_time(top, tr[0].need_by, comp_id):
            # One supplier can meet every date. Use it for everything (one purchase order)
            # unless splitting by deadline is more than reuse_pct cheaper.
            single = [Line(i, top, t.qty) for i, t in enumerate(tr)]
            if self._cost(single) <= (1 + self.reuse_pct) * self._cost(split_lines) + EPS:
                extra = self._cost(single) / self._cost(split_lines) - 1 if self._cost(split_lines) else 0
                dec.lines = single
                dec.choice_reasons = {key(top): why if extra <= EPS else
                                      f"{why}; one supplier for all deliveries costs {extra:.0%} more than splitting "
                                      f"(within the {self.reuse_pct:.0%} allowance)"}

        self._apply_moq(dec)
        if dec.concentration and dec.concentration.params.get("applies_per_order"):
            self._enforce_per_order_concentration(dec)
        for l in dec.lines:
            if not l.option.domestic:
                dec.intl_reasons[key(l.option)] = self.intl_reason(l.option, tr[l.tranche].need_by, eligible, comp_id) \
                    or "needed to satisfy the supplier-concentration rule"
        return dec

    def _cost(self, lines: list[Line]) -> float:
        trial = [replace(l) for l in lines]
        self._moq_fix(trial)
        return sum(l.qty * l.option.unit_price for l in trial)

    def _split_by_deadline(self, req: Requirement, eligible: list[SupplierOption]) -> tuple[list[Line], dict]:
        """Best supplier per deadline, reusing an earlier supplier when within reuse_pct on price."""
        comp_id, lines, reasons = req.component_id, [], {}
        for i, t in enumerate(req.tranches):
            choice, why = self.rank(eligible, t.need_by, t.qty, comp_id)[0]
            for o in {key(l.option): l.option for l in lines}.values():
                together = t.qty + sum(l.qty for l in lines if key(l.option) == key(o))
                if key(o) != key(choice) and self.on_time(o, t.need_by, comp_id) \
                        and self.intl_reason(o, t.need_by, eligible, comp_id) is not None \
                        and self.eff_price(o, together) <= (1 + self.reuse_pct) * self.eff_price(choice, t.qty) + EPS:
                    choice, why = o, f"same supplier as the earlier delivery; price within {self.reuse_pct:.0%} of the best option"
                    break
            lines.append(Line(i, choice, t.qty))
            reasons.setdefault(key(choice), why)
        return lines, reasons

    # ---- MOQ ------------------------------------------------------------------------------
    @staticmethod
    def _moq_fix(lines: list[Line]) -> dict[tuple, int]:
        """Raise any supplier below its MOQ up to it (on its latest line). Returns surplus per supplier."""
        surplus: dict[tuple, int] = {}
        totals: dict[tuple, int] = {}
        for l in lines:
            totals[key(l.option)] = totals.get(key(l.option), 0) + l.qty
        for k, qty in totals.items():
            last = max((l for l in lines if key(l.option) == k), key=lambda l: l.tranche)
            if 0 < qty < last.option.moq:
                surplus[k] = last.option.moq - qty
                last.qty += surplus[k]
        return surplus

    def _apply_moq(self, dec: ComponentDecision) -> None:
        dec.lines = [l for l in dec.lines if l.qty > 0]
        dec.surplus = self._moq_fix(dec.lines)

    # ---- per-order concentration (e.g. the magnet memo) ------------------------------------
    def _existing_by_supplier(self, comp_id: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.state.existing_pos:
            if p.component_id == comp_id:
                out[p.supplier_id] = out.get(p.supplier_id, 0) + p.quantity
        return out

    def _score(self, dec: ComponentDecision, lines: list[Line], follow_rule: bool) -> tuple:
        p = dec.concentration.params
        cap, min_sec = p.get("max_share", 1.0), p.get("min_secondary_share", 0.0)
        totals = dict(self._existing_by_supplier(dec.component_id))
        for l in lines:
            totals[l.option.supplier.supplier_id] = totals.get(l.option.supplier.supplier_id, 0) + l.qty
        T = sum(totals.values())
        over = sum(max(0, v - math.floor(cap * T + EPS)) for v in totals.values())
        sec_short = max(0, math.ceil(min_sec * T - EPS) - (T - max(totals.values()))) if T else 0
        tr = dec.requirement.tranches
        # A production order can only start when its LAST unit arrives, so lateness is measured
        # per deadline (the latest arrival serving it), not per unit.
        late_by_tranche: dict[int, int] = {}
        for l in lines:
            d = max(0, (self.ready(l.option, dec.component_id) - tr[l.tranche].need_by).days)
            late_by_tranche[l.tranche] = max(late_by_tranche.get(l.tranche, 0), d)
        late = sum(late_by_tranche.values())
        cost = sum(l.qty * l.option.unit_price for l in lines)
        broken = over + sec_short
        return (broken, late, cost) if follow_rule else (late, broken, cost)

    def _search(self, dec: ComponentDecision, start: list[Line], follow_rule: bool) -> list[Line]:
        """Local search: try every small change, keep the one that improves the score most,
        repeat until nothing improves. Changes are (a) move some units from one supplier to
        another for the same deadline, or (b) buy a few extra units from a supplier, either
        1-3 (an odd total cannot be split exactly 50/50) or exactly enough to bring the
        largest supplier down to the cap."""
        lines = [replace(l) for l in start]
        eligible = dec.eligible
        tr = dec.requirement.tranches
        last = len(tr) - 1
        need = dec.requirement.to_buy
        base = [replace(l) for l in start]
        self._moq_fix(base)
        # extra units allowed: whatever minimum orders already force, or the configured share of need
        max_total = need + max(sum(l.qty for l in base) - need, math.ceil(self.max_extra_pct * need)) + 3
        for _ in range(200):
            best, best_score = None, self._score(dec, lines, follow_rule)
            trials = []
            for li, l in enumerate(lines):
                for alt in eligible:
                    if key(alt) == key(l.option):
                        continue
                    if self.intl_reason(alt, tr[l.tranche].need_by, eligible, dec.component_id) is None:
                        continue
                    for amount in {l.qty, *range(1, l.qty + 1, max(1, l.qty // 20))}:
                        trials.append((li, l.tranche, alt, amount))
            cap = dec.concentration.params.get("max_share", 1.0)
            totals = dict(self._existing_by_supplier(dec.component_id))
            for x in lines:
                totals[x.option.supplier.supplier_id] = totals.get(x.option.supplier.supplier_id, 0) + x.qty
            T = sum(totals.values())
            for alt in eligible:
                if self.intl_reason(alt, tr[last].need_by, eligible, dec.component_id) is None:
                    continue
                others = [v for sid, v in totals.items() if sid != alt.supplier.supplier_id]
                # the extra volume that would bring the biggest other supplier down to the cap
                needed = math.ceil(max(others) / cap - T - EPS) if others and cap < 1 else 0
                for extra in {1, 2, 3, needed}:
                    if extra > 0:
                        trials.append((None, last, alt, extra))
            for li, tranche, alt, amount in trials:
                trial = [replace(x) for x in lines]
                if li is not None:
                    trial[li].qty -= amount
                tgt = next((x for x in trial if x.tranche == tranche and key(x.option) == key(alt)), None)
                if tgt:
                    tgt.qty += amount
                else:
                    trial.append(Line(tranche, alt, amount))
                trial = [x for x in trial if x.qty > 0]
                self._moq_fix(trial)
                if sum(x.qty for x in trial) > max_total:
                    continue  # never buy lots of unneeded stock just to satisfy a split rule
                sc = self._score(dec, trial, follow_rule)
                if sc < best_score:
                    best, best_score = trial, sc
            if best is None:
                break
            lines = best
        return lines

    def _enforce_per_order_concentration(self, dec: ComponentDecision) -> None:
        # undo the MOQ bumps so the search starts from true need
        for k, extra in dec.surplus.items():
            last = max((l for l in dec.lines if key(l.option) == k), key=lambda l: l.tranche)
            last.qty -= extra
        dec.lines = [l for l in dec.lines if l.qty > 0]
        other = self._search(dec, dec.lines, not self.follow_rule)
        # search from two starting points and keep the better result (avoids getting stuck)
        candidates = [self._search(dec, dec.lines, self.follow_rule), self._search(dec, other, self.follow_rule)]
        chosen = min(candidates, key=lambda ls: self._score(dec, ls, self.follow_rule))

        # the search starts below minimum order quantities; if no split beat the start, it is
        # returned as is, so minimums are applied again here (the stress test caught this)
        self._moq_fix(chosen)
        before = {key(l.option) for l in dec.lines}
        dec.lines = chosen
        dec.surplus = {}
        bought: dict[tuple, int] = {}
        for l in chosen:
            bought[key(l.option)] = bought.get(key(l.option), 0) + l.qty
        for l in chosen:
            if key(l.option) not in before:
                dec.choice_reasons.setdefault(key(l.option), f"allocated to meet the concentration rule ({dec.concentration.citation})")
        total_need = dec.requirement.to_buy
        extra = sum(bought.values()) - total_need
        if extra > 0:
            k = max(bought, key=lambda k: next(l.option.moq for l in chosen if key(l.option) == k))
            dec.surplus[k] = extra

        p = dec.concentration.params
        sc_rule = self._score(dec, chosen, True)
        if sc_rule[0] > 0:
            totals = dict(self._existing_by_supplier(dec.component_id))
            for l in chosen:
                totals[l.option.supplier.supplier_id] = totals.get(l.option.supplier.supplier_id, 0) + l.qty
            T = sum(totals.values())
            top = max(totals, key=totals.get)
            dec.conflicts.append(
                f"{self.state.suppliers[top].name} ({top}) ends up with {totals[top] / T:.0%} of open + new volume, above the "
                f"{p.get('max_share', 1):.0%} cap ({dec.concentration.citation}). This is the closest achievable split: moving "
                f"more volume would break another rule or force large minimum-order surpluses.")
        mine, theirs = self._score(dec, chosen, True), self._score(dec, other, True)
        if self.follow_rule and theirs[1] < mine[1] and theirs[0] > mine[0]:
            sid, share = self._top_share(dec, other)
            dec.alternative = (f"Protecting the schedule instead would cut total lateness from {mine[1]} to {theirs[1]} days "
                               f"but leave {self.state.suppliers[sid].name} ({sid}) with {share:.0%} of volume "
                               f"(cap {p.get('max_share', 1):.0%}).")
        elif not self.follow_rule and theirs[0] < mine[0]:
            dec.alternative = f"Following the concentration rule instead would add {theirs[1] - mine[1]} days of lateness."

    def _top_share(self, dec: ComponentDecision, lines: list[Line]) -> tuple[str, float]:
        totals = dict(self._existing_by_supplier(dec.component_id))
        for l in lines:
            totals[l.option.supplier.supplier_id] = totals.get(l.option.supplier.supplier_id, 0) + l.qty
        sid = max(totals, key=totals.get)
        return sid, totals[sid] / sum(totals.values())

    # ---- output ---------------------------------------------------------------------------
    def purchase_orders(self, dec: ComponentDecision) -> list[PlannedPO]:
        remaining = [[list(x) for x in t.orders] for t in dec.requirement.tranches]
        pos: dict[tuple, PlannedPO] = {}
        for l in sorted(dec.lines, key=lambda l: (l.tranche, self.ready(l.option, dec.component_id))):
            o = l.option
            po = pos.setdefault(key(o), PlannedPO(dec.component_id, o.supplier.supplier_id, 0, o.unit_price,
                                                  o.order_date, o.arrival, o.mode))
            po.quantity += l.qty
            need = l.qty
            for item in remaining[l.tranche]:
                take = min(item[1], need)
                if take > 0:
                    po.serves.append((item[0], take))
                    item[1] -= take
                    need -= take
        return list(pos.values())
