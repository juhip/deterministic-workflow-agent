"""Typed containers for the scenario state. No behaviour lives here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Component:
    component_id: str
    name: str
    description: str
    category: str
    unit_of_measure: str
    is_hazardous: bool
    requires_certification: tuple[str, ...]  # as stored in the DB column (often incomplete)


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    name: str
    country: str
    is_domestic: bool  # the DB's own flag; the policy defines "domestic" differently (US + Mexico)
    certifications: tuple[str, ...]
    sustainability_rating: str
    relationship_tier: str
    on_approved_list: bool
    notes: str


@dataclass(frozen=True)
class CatalogEntry:
    supplier_id: str
    component_id: str
    unit_price: float
    lead_time_days: int
    minimum_order_qty: int
    notes: str


@dataclass(frozen=True)
class ProductionOrder:
    order_id: str
    product_id: str
    quantity: int
    customer: str
    materials_needed_by: date


@dataclass(frozen=True)
class ExistingPO:
    po_number: str
    component_id: str
    supplier_id: str
    quantity: float
    unit_price: Optional[float]
    order_date: Optional[date]
    expected_delivery_date: Optional[date]
    rationale: str


@dataclass
class ScenarioState:
    """Everything the agent knows about the world for one scenario."""
    path: str
    current_date: date
    description: str
    products: dict[str, dict]
    components: dict[str, Component]
    bom: dict[str, dict[str, float]]   # product_id -> component_id -> quantity_per
    suppliers: dict[str, Supplier]
    catalog: list[CatalogEntry]
    inventory: dict[str, float]        # component_id -> quantity on hand
    schedule: list[ProductionOrder]    # sorted by (materials_needed_by, order_id)
    existing_pos: list[ExistingPO]
    existing_alert_count: int = 0
    issues: list[str] = field(default_factory=list)  # data problems found while loading

    def catalog_for(self, component_id: str) -> list[CatalogEntry]:
        return [e for e in self.catalog if e.component_id == component_id]
