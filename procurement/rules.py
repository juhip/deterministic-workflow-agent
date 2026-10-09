"""Load and query the rulebook (rules/policy_rules.json).

The code here knows *kinds* of rule (a certification requirement, a concentration limit,
a shipping authorization with a date window...). The JSON says which parts and suppliers
each rule covers, its numbers, its dates and where it came from. That split is what lets a
new memo be handled by editing data rather than code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import Component

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[1] / "rules" / "policy_rules.json"

KNOWN_TYPES = {
    "approved_supplier_list", "required_certification", "domestic_definition", "domestic_preference",
    "critical_classification", "concentration_limit", "minimum_order_quantity", "hazmat_handling",
    "approval_thresholds", "sustainability_preference", "strategic_preference", "delivery_date",
    "expedited_shipping", "supplier_qualification_freeze", "shipment_documentation",
}
SELECTOR_KEYS = {"any_keywords", "name_keywords", "all_of", "category_in", "id_aliases", "hazardous"}


class RulebookError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    type: str
    source: dict
    effective_from: date
    effective_to: date | None
    scope: dict
    params: dict
    overrides: str | None = None
    notes: str = ""

    @property
    def citation(self) -> str:
        sec = self.source.get("section")
        return f"{self.source['document']}" + (f" §{sec}" if sec else "")

    def active_on(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)

    def applies_to(self, comp: Component) -> bool:
        return matches(self.scope.get("selector", {}), comp)


def _kw(keyword: str, text: str) -> bool:
    # whole-word match, so "pcb" does not match inside another word
    return re.search(r"\b" + re.escape(keyword.lower()) + r"\b", text) is not None


def matches(selector: dict, comp: Component) -> bool:
    """Does a component fall inside a rule's scope?  An empty selector matches everything.

    any_keywords : at least one keyword appears in the name/description
    name_keywords: at least one keyword appears in the NAME (use when descriptions mention the
                   part loosely, e.g. a coating described as a "protective spray for circuit boards")
    all_of       : a list of keyword groups; every group needs at least one hit
    category_in  : the component's category is one of these
    id_aliases   : IDs used in the documents (e.g. memo IDs) that may appear in some databases
    hazardous    : the component's hazardous flag equals this value
    """
    text = f"{comp.name} {comp.description}".lower()
    if comp.component_id in selector.get("id_aliases", []):
        return True
    if "any_keywords" in selector and not any(_kw(k, text) for k in selector["any_keywords"]):
        return False
    if "name_keywords" in selector and not any(_kw(k, comp.name.lower()) for k in selector["name_keywords"]):
        return False
    for group in selector.get("all_of", []):
        if not any(_kw(k, text) for k in group):
            return False
    if "category_in" in selector and comp.category not in selector["category_in"]:
        return False
    if "hazardous" in selector and comp.is_hazardous != selector["hazardous"]:
        return False
    return True


def _date(value, where: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise RulebookError(f"{where}: bad date {value!r}")


class Rulebook:
    def __init__(self, data: dict, extra_scope: dict[str, set[str]] | None = None):
        self.raw = data
        # part -> rule ids the (verified) model says apply, on top of the keyword selectors
        self.extra_scope: dict[str, set[str]] = extra_scope or {}
        self.rating_scale: list[str] = data.get("rating_scale_best_first", [])
        self.rules: list[Rule] = [self._parse(r) for r in data.get("rules", [])]
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise RulebookError("duplicate rule ids")
        for r in self.rules:
            if r.overrides and r.overrides not in ids:
                raise RulebookError(f"{r.id}: overrides unknown rule {r.overrides}")

    def base_policy_in_force(self, day: date) -> tuple["Rulebook", str | None]:
        """A scenario dated before the base policy took effect would otherwise run with NO policy at all
        (not even the approved-supplier list). No earlier version was provided, so apply the current one
        and say so. Memos keep their own dates: a memo not yet issued stays out of force."""
        doc = self.raw.get("settings", {}).get("base_policy_document")
        early = [r for r in self.rules if doc and r.source.get("document") == doc and r.effective_from > day]
        if not early:
            return self, None
        import copy
        raw = copy.deepcopy(self.raw)
        for r in raw["rules"]:
            if r["source"].get("document") == doc and _date(r["effective_from"], r["id"]) > day:
                r["effective_from"] = day.isoformat()
        took_effect = min(r.effective_from for r in early)
        return Rulebook(raw, self.extra_scope), (f"The scenario date {day} is before {doc} took effect ({took_effect}). No earlier version "
                               f"was provided, so the agent applied the current one. Please confirm which policy was in "
                               f"force on this date.")

    @classmethod
    def load(cls, path: str | Path = DEFAULT_RULES_PATH) -> "Rulebook":
        return cls(json.loads(Path(path).read_text()))

    @staticmethod
    def _parse(r: dict) -> Rule:
        rid = r.get("id", "?")
        for key in ("id", "type", "source", "effective_from", "scope", "params"):
            if key not in r:
                raise RulebookError(f"rule {rid}: missing '{key}'")
        if r["type"] not in KNOWN_TYPES:
            raise RulebookError(f"rule {rid}: unknown type {r['type']!r} (the code has no logic for it)")
        if "document" not in r["source"]:
            raise RulebookError(f"rule {rid}: source must name a document")
        bad = set(r["scope"].get("selector", {})) - SELECTOR_KEYS
        if bad:
            raise RulebookError(f"rule {rid}: unknown selector keys {sorted(bad)}")
        start, end = _date(r["effective_from"], rid), _date(r.get("effective_to"), rid)
        if end and end < start:
            raise RulebookError(f"rule {rid}: effective_to is before effective_from")
        return Rule(rid, r["type"], r["source"], start, end, r["scope"], r["params"],
                    r.get("overrides"), r.get("notes", ""))

    # ---- queries ------------------------------------------------------------------------
    def active(self, rule_type: str, day: date) -> list[Rule]:
        """Rules of this type in force on `day`. A rule that applies to everything (empty
        selector) and names `overrides` replaces the overridden rule completely; a scoped
        override only replaces it for the parts in its scope (see for_component)."""
        found = [r for r in self.rules if r.type == rule_type and r.active_on(day)]
        replaced = {r.overrides for r in found if r.overrides and not r.scope.get("selector")}
        return [r for r in found if r.id not in replaced]

    def one(self, rule_type: str, day: date) -> Rule | None:
        found = self.active(rule_type, day)
        return found[0] if found else None

    def covers(self, rule: Rule, comp: Component) -> bool:
        """The rule's keyword selector matches the part, or the model's verified labels say it applies."""
        return rule.applies_to(comp) or rule.id in self.extra_scope.get(comp.component_id, ())

    def with_extra_scope(self, extra: dict[str, set[str]]) -> "Rulebook":
        return Rulebook(self.raw, {cid: set(ids) for cid, ids in extra.items()}) if extra else self

    def for_component(self, rule_type: str, comp: Component, day: date) -> list[Rule]:
        """Active rules of this type covering the component, with overridden rules removed."""
        found = [r for r in self.active(rule_type, day) if self.covers(r, comp)]
        overridden = {r.overrides for r in found if r.overrides}
        return [r for r in found if r.id not in overridden]

    def rating_rank(self, rating: str) -> int:
        """Higher is better. Unknown ratings rank lowest."""
        scale = [x.upper() for x in self.rating_scale]
        r = (rating or "").strip().upper()
        return len(scale) - scale.index(r) if r in scale else 0
