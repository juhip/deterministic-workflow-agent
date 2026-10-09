"""Exception handling: when a production order will be late, work out how to recover.

Code builds the options and checks each one; an optional model recommends one; a person decides.
Code builds them because every option must be verified: would it make the date, what would it
cost, which rule would it bend? Each option type is one code can test:

  waive a rule       re-run the whole agent with that rule switched off for today and compare
                     (only rule types listed in settings.waivable_rule_types; the approved-supplier
                     list and certificates are never waivable)
  extend a memo      re-run with an expired memo still in force (settings.extendable_rule_types),
                     e.g. the air-freight authorization
  build part         units that can be built on time, and the revenue they carry
  move the start     the date the last material arrives, at no extra cost
  ask to expedite    the supplier order that sets the date, and how many days sooner it is needed
                     (needs the supplier's confirmation: there is no data on whether they can)

An option that does not improve the order is dropped. Nothing here changes the plan: the options
are advice for the person who decides, written as one ACTION alert per late order.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .alerts import Alert
from .llm import LLMError, ModelProvider
from .models import ScenarioState
from .release import conflict_decider
from .rules import Rule, Rulebook

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "exception_advisor.md"


@dataclass
class Option:
    letter: str
    text: str


@dataclass
class Exception_:
    order_id: str
    header: str
    options: list[Option] = field(default_factory=list)


def _status(result, order_id):
    return next(s for s in result.statuses if s.order.order_id == order_id)


def _better(new, old) -> bool:
    if new.on_time:
        return True
    if new.ready is None:
        return new.buildable_on_time > old.buildable_on_time
    return old.ready is None or (new.late_days or 0) < (old.late_days or 0) or new.buildable_on_time > old.buildable_on_time


def _outcome(st) -> str:
    if st.on_time:
        return "the order is on time"
    if st.ready is None:
        return f"still short; {st.buildable_on_time} of {st.order.quantity} units can be built"
    return f"still {st.late_days} day(s) late"


def _rerun(state: ScenarioState, raw: dict, labels=None):
    from .pipeline import run                     # local import: pipeline imports this module
    return run(state, Rulebook(raw), None, explore=False, part_labels=labels)


def _rule_variants(state: ScenarioState, rules: Rulebook, comps: list[str]):
    """(rule, kind, changed rulebook) for each rule that could be waived or extended for these parts."""
    today = state.current_date
    settings = rules.raw.get("settings", {})
    waivable, extendable = set(settings.get("waivable_rule_types", [])), set(settings.get("extendable_rule_types", []))
    seen = set()
    for cid in comps:
        comp = state.components[cid]
        for rtype in sorted(waivable):
            for rule in rules.for_component(rtype, comp, today):
                if rule.id in seen or (rtype == "concentration_limit" and not rule.params.get("applies_per_order")):
                    continue
                if any(r.overrides == rule.id for r in rules.rules):
                    continue                              # other rules build on it; waiving it is not a clean test
                seen.add(rule.id)
                raw = copy.deepcopy(rules.raw)
                raw["rules"] = [r for r in raw["rules"] if r["id"] != rule.id]     # waived for this test run only
                yield rule, "waive", raw
    for rule in rules.rules:
        if rule.type in extendable and rule.effective_to and rule.effective_to < today and rule.id not in seen:
            seen.add(rule.id)
            raw = copy.deepcopy(rules.raw)
            next(r for r in raw["rules"] if r["id"] == rule.id)["effective_to"] = str(today)
            yield rule, "extend", raw


def _cost(result, comps) -> float:
    return sum(p.total for p in result.pos if p.component_id in comps)


def _share_note(state, result, cid) -> str:
    vol: dict[str, float] = {}
    for p in state.existing_pos:
        if p.component_id == cid:
            vol[p.supplier_id] = vol.get(p.supplier_id, 0) + p.quantity
    for p in result.pos:
        if p.component_id == cid:
            vol[p.supplier_id] = vol.get(p.supplier_id, 0) + p.quantity
    if not vol:
        return ""
    sid = max(vol, key=vol.get)
    return f"{state.suppliers[sid].name} would hold {vol[sid] / sum(vol.values()):.0%} of {state.components[cid].name} volume"


def _describe(rules: Rulebook, rule: Rule) -> str:
    raw = next((r for r in rules.raw["rules"] if r["id"] == rule.id), {})
    return raw.get("title") or rule.scope.get("description", rule.type.replace("_", " "))


def recovery_options(state: ScenarioState, rules: Rulebook, result) -> list[Exception_]:
    out: list[Exception_] = []
    decider = conflict_decider(rules)
    for st in result.statuses:
        if st.on_time:
            continue
        o = st.order
        product = state.products.get(o.product_id) or {}
        price, pname = product.get("unit_price"), product.get("name", o.product_id)
        comps = [c for c, _ in st.limiting]
        short = o.quantity - st.buildable_on_time
        risk = f", ${short * price:,.0f} of revenue at risk" if price and short > 0 else ""
        late = f"{st.late_days} day(s) late" if st.ready else "cannot be fully supplied"
        ex = Exception_(o.order_id, f"Recovery options for {o.order_id} ({o.customer}, {o.quantity} x {pname}), "
                                    f"{late}{risk}. Each option was checked by the agent against the policy:")
        texts = []
        base_cost = _cost(result, comps)
        for rule, kind, raw in _rule_variants(state, rules, comps):
            alt = _rerun(state, raw, result.part_labels)
            new = _status(alt, o.order_id)
            if not _better(new, st):
                continue
            delta = _cost(alt, comps) - base_cost
            money = (f"parts cost ${delta:,.2f} more" if delta > 0.005 else
                     f"parts cost ${-delta:,.2f} less" if delta < -0.005 else "no change in parts cost")
            if kind == "waive":
                notes = "; ".join(n for n in (_share_note(state, alt, c) for c in comps) if n)
                texts.append(f"Waive {rule.citation} ({_describe(rules, rule)}) for this order: {_outcome(new)}; {money}"
                             + (f"; {notes}" if notes else "") + f". Needs an exception from the {decider}.")
            else:
                approver = rule.params.get("approver") or decider
                texts.append(f"Extend {rule.citation} ({_describe(rules, rule)}) for this order: {_outcome(new)}; {money}"
                             + ("; freight cost not known (no unit weights)" if rule.type == "expedited_shipping" else "")
                             + f". Needs approval from the {approver}.")
        if st.ready and 0 < st.buildable_on_time < o.quantity:
            on_time_rev = f" (${st.buildable_on_time * price:,.0f} of revenue on time)" if price else ""
            texts.append(f"Build {st.buildable_on_time} of {o.quantity} units by {o.materials_needed_by}{on_time_rev}, "
                         f"and the other {short} by {st.ready}. No extra cost; {o.customer} must accept a split delivery.")
        if st.ready:
            texts.append(f"Move the start to {st.ready} ({st.late_days} day(s) later) and tell {o.customer}. No extra cost.")
        else:                                          # some part cannot be bought at all
            for cid in comps:
                dec = result.decisions.get(cid)
                if dec is not None and dec.no_eligible_supplier:
                    texts.append(f"Source {state.components[cid].name} ({cid}) from a new supplier: no supplier the policy "
                                 f"allows sells it today. The agent cannot check this option; it needs a qualified "
                                 f"supplier added to the approved list.")
            if st.buildable_on_time > 0:
                texts.append(f"Build {st.buildable_on_time} of {o.quantity} units by {o.materials_needed_by} from parts "
                             f"in hand; the rest waits until the missing parts are sourced.")
        asks: dict[str, list] = {}                     # supplier -> [(part, arrives)] for orders feeding THIS order
        for cid, _ in st.limiting:
            feeding = [p for p in result.pos if p.component_id == cid and o.order_id in {oid for oid, _ in p.serves}
                       and p.expected_delivery_date > o.materials_needed_by]
            for p in feeding:
                asks.setdefault(p.supplier_id, []).append((state.components[cid].name, p.expected_delivery_date))
        for sid, items in asks.items():
            latest = max(d for _, d in items)
            parts = ", ".join(sorted({n for n, _ in items}))
            texts.append(f"Ask {state.suppliers[sid].name} to deliver {parts} by {o.materials_needed_by} instead of "
                         f"{latest} ({(latest - o.materials_needed_by).days} day(s) sooner). "
                         "Needs the supplier's confirmation; cost unknown.")
        if not texts:
            texts.append("None the agent can check from the data; the order needs a decision.")
        ex.options = [Option(chr(ord("A") + i), t) for i, t in enumerate(texts)]
        if ex.options:
            out.append(ex)
    return out


def as_alert(ex: Exception_, rules: Rulebook, recommendation: str = "") -> Alert:
    body = " ".join(f"({opt.letter}) {opt.text}" for opt in ex.options)
    return Alert("ACTION", f"{ex.header} {body} Decision: the {conflict_decider(rules)}.{recommendation}")


def recommend(client: ModelProvider, state: ScenarioState, rules: Rulebook,
              exceptions: list[Exception_]) -> tuple[dict[str, str], Alert]:
    """Ask the model to pick one option per exception and say why. Code rejects a pick that is not on
    the list, and a reason that brings in any number, date or name not in that exception's options."""
    from .messages import check_invented, known_names
    payload = [{"id": ex.order_id, "situation": ex.header,
                "options": [{"option": o.letter, "facts": o.text} for o in ex.options]} for ex in exceptions]
    try:
        raw = client.chat([{"role": "system", "content": PROMPT.read_text()},
                           {"role": "user", "content": "EXCEPTIONS\n" + json.dumps(payload, indent=1)}])
        m = re.search(r"\{.*\}", raw, re.S)
        reply = json.loads(m.group(0)) if m else None
    except (LLMError, ValueError) as e:
        return {}, Alert("INFO", f"AI option advisor skipped ({str(e)[:100]}); options are listed without a recommendation.")
    if not isinstance(reply, dict) or not isinstance(reply.get("recommendations"), list):
        return {}, Alert("INFO", "AI option advisor returned an unusable answer; options are listed without a recommendation.")
    names = known_names(state, rules)
    by_id = {ex.order_id: ex for ex in exceptions}
    picks, rejected = {}, []
    for r in reply["recommendations"]:
        if not isinstance(r, dict) or r.get("id") not in by_id:
            continue
        ex = by_id[r["id"]]
        letter = str(r.get("option", "")).strip().upper()
        reason = " ".join(str(r.get("reason", "")).split())[:400]
        if letter not in {o.letter for o in ex.options}:
            rejected.append(f"{ex.order_id} (no option {letter or '?'})")
            continue
        problem = check_invented(ex.header + " " + " ".join(o.text for o in ex.options), reason, names)
        if problem or not reason:
            rejected.append(f"{ex.order_id} ({problem or 'no reason given'})")
            continue
        picks[ex.order_id] = f" Recommended by {client.model_name}: option {letter}. {reason}"
    note = (f"AI option advisor ({client.model_name}) recommended an option for {len(picks)} of {len(exceptions)} late "
            f"order(s)" + (f"; rejected {len(rejected)}: {'; '.join(rejected)}" if rejected else "")
            + ". The person deciding sees every checked option either way.")
    return picks, Alert("INFO", note)
