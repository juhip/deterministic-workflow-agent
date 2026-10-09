"""For one component: every supplier in the catalog, whether we may use it (and if not, why),
and when an order placed today would arrive.

Only *hard* rules live here: things that make a supplier unusable. Preferences (price,
domestic, sustainability, Strategic tier, concentration) are judged later by the decider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .classifier import ComponentTags
from .models import CatalogEntry, ScenarioState, Supplier
from .rules import Rulebook


def _norm(cert: str) -> str:
    return "".join(ch for ch in cert.lower() if ch.isalnum())


@dataclass(frozen=True)
class SupplierOption:
    supplier: Supplier
    entry: CatalogEntry
    mode: str                       # "standard" | "air"
    lead_days: int
    order_date: date
    arrival: date
    domestic: bool
    excluded_because: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()     # things a human must know/approve, e.g. air freight

    @property
    def eligible(self) -> bool:
        return not self.excluded_because

    @property
    def unit_price(self) -> float:
        return self.entry.unit_price

    @property
    def moq(self) -> int:
        return max(1, self.entry.minimum_order_qty)


def is_domestic(supplier: Supplier, rules: Rulebook, today: date) -> bool:
    """Policy definition wins over the DB flag (the policy counts Mexico as domestic)."""
    rule = rules.one("domestic_definition", today)
    if rule and supplier.country:
        return supplier.country.strip().lower() in {c.lower() for c in rule.params["countries"]}
    return supplier.is_domestic


def exclusion_reasons(supplier: Supplier, tags: ComponentTags, state: ScenarioState,
                      rules: Rulebook, comp_id: str) -> list[str]:
    today = state.current_date
    comp = state.components[comp_id]
    why: list[str] = []

    asl = rules.one("approved_supplier_list", today)
    if asl and not supplier.on_approved_list:
        why.append(f"not on the Approved Supplier List ({asl.citation})")

    held = {_norm(c) for c in supplier.certifications}
    missing = [c for c in tags.required_certs if _norm(c) not in held]
    if missing:
        cites = sorted({cite for c in missing for cite in tags.required_certs[c]})
        why.append(f"lacks required certification {', '.join(missing)} ({'; '.join(cites)})")

    for freeze in rules.for_component("supplier_qualification_freeze", comp, today):
        ev = freeze.params.get("evidence_of_prior_receipt", {})
        prior_po = ev.get("prior_purchase_order") and any(
            p.supplier_id == supplier.supplier_id and p.component_id == comp_id for p in state.existing_pos)
        tier_ok = supplier.relationship_tier in ev.get("relationship_tier_in", [])
        if not (prior_po or tier_ok):
            why.append(f"new-supplier freeze: no evidence we have received this part from them before ({freeze.citation})")
    return why


def build_options(state: ScenarioState, rules: Rulebook, tags: ComponentTags, comp_id: str) -> list[SupplierOption]:
    """All catalog offers for the component, eligible or not, standard and (if allowed) air freight."""
    today = state.current_date
    out: list[SupplierOption] = []
    air_rules = rules.active("expedited_shipping", today)
    for entry in state.catalog_for(comp_id):
        sup = state.suppliers[entry.supplier_id]
        dom = is_domestic(sup, rules, today)
        why = tuple(exclusion_reasons(sup, tags, state, rules, comp_id))
        arrive = today + timedelta(days=entry.lead_time_days)
        out.append(SupplierOption(sup, entry, "standard", entry.lead_time_days, today, arrive, dom, why))

        for air in air_rules:
            if air.params.get("international_only") and dom:
                continue
            p = air.params
            air_days = max(p["min_lead_time_days"], entry.lead_time_days - p["lead_time_reduction_days"])
            if air_days >= entry.lead_time_days:
                continue  # air would not be faster; no point offering it
            flags = (f"air freight under {air.citation}: needs {p['approver']} approval"
                     f"{' for each request' if p.get('approval_per_request') else ''}; "
                     f"counts against the ${p['budget_cap']:,} cap",)
            out.append(SupplierOption(sup, entry, "air", air_days, today, today + timedelta(days=air_days),
                                      dom, why, flags))
    return out
