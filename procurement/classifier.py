"""Label each component using the rulebook: is it critical, which certifications must its
suppliers hold, which rules (including memos) apply to it, is it hazardous.

The keyword rules in the rulebook are the safety floor. When a model is connected it reads every
part first (part_labels.py); its verified labels are merged here: a part is critical if either
says so, and a rule applies if either says so (Rulebook.covers). Every label keeps the reason it
was given, so it can be quoted in a PO rationale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Component
from .rules import Rulebook, matches


@dataclass
class ComponentTags:
    component_id: str
    critical: bool
    critical_reason: str = ""                     # e.g. "samarium-cobalt magnet (HF-PUR-100 §4)"
    critical_note: str = ""                       # interpretation caveat, if any
    required_certs: dict[str, list[str]] = field(default_factory=dict)  # cert -> citations
    hazardous: bool = False
    rule_ids: list[str] = field(default_factory=list)                   # component-scoped rules that apply
    alias_notes: list[str] = field(default_factory=list)                # "memo says ITM-4403, matched by name"


def classify(comp: Component, rules: Rulebook, today: date, model_label: str | None = None) -> ComponentTags:
    tags = ComponentTags(comp.component_id, critical=False, hazardous=comp.is_hazardous)

    crit = rules.one("critical_classification", today)
    if crit:
        for cat in crit.params.get("categories", []):
            if matches(cat["selector"], comp):
                tags.critical = True
                tags.critical_reason = f"{cat['label']} ({crit.citation})"
                tags.critical_note = cat.get("note", "")
                break
        if not tags.critical and model_label:
            tags.critical = True
            tags.critical_reason = f"{model_label} ({crit.citation}, read by the model)"

    # certifications: union of the DB column and every applicable policy rule
    for cert in comp.requires_certification:
        tags.required_certs.setdefault(cert, []).append("component master data")
    for rule in rules.for_component("required_certification", comp, today):
        for cert in rule.params["certifications"]:
            tags.required_certs.setdefault(cert, []).append(rule.citation)

    for rule in rules.rules:
        if not rule.active_on(today) or not rule.scope.get("selector"):
            continue  # only rules with a specific scope are interesting per component
        if rules.covers(rule, comp):
            tags.rule_ids.append(rule.id)
            aliases = rule.scope["selector"].get("id_aliases", [])
            note_needed = aliases and comp.component_id not in aliases and rule.applies_to(comp)
            if note_needed and not any(rule.source["document"] in n for n in tags.alias_notes):
                tags.alias_notes.append(
                    f"{rule.source['document']} refers to {'/'.join(aliases)}; applied to {comp.component_id} "
                    f"({comp.name}) by name match")
    return tags


def classify_all(components: dict[str, Component], rules: Rulebook, today: date,
                 model_labels: dict[str, str] | None = None) -> dict[str, ComponentTags]:
    model_labels = model_labels or {}
    return {cid: classify(c, rules, today, model_labels.get(cid)) for cid, c in components.items()}
