"""Thin service layer over `hiveflow.dna.industry_mapping` / `industry_templates`
for the portal UI. Every mutating function loads the persisted mapping, applies
one engine call, then persists it back — the engine module owns all the actual
approve/reject/exclude/materialize logic.
"""

from __future__ import annotations

from typing import Any

from hiveflow.dna import industry_mapping
from hiveflow.dna.industry_mapping import ClientModelMapping, load_mapping, save_mapping
from hiveflow.dna.industry_templates import (
    IndustryTemplate,
    list_available_industry_templates,
    load_industry_template_by_id,
)
from hiveflow.dna.settings import DnaSettings


def available_templates() -> list[str]:
    return list_available_industry_templates()


def load_current_mapping(settings: DnaSettings) -> ClientModelMapping | None:
    return load_mapping(settings)


def load_template_for(mapping: ClientModelMapping) -> IndustryTemplate:
    return load_industry_template_by_id(mapping.industry_pack_id)


def _require_mapping(settings: DnaSettings) -> ClientModelMapping:
    mapping = load_mapping(settings)
    if mapping is None:
        raise ValueError("No model mapping initialized yet — pick an industry to get started.")
    return mapping


def initialize(settings: DnaSettings, industry_pack_id: str) -> ClientModelMapping:
    template = load_industry_template_by_id(industry_pack_id)
    mapping = industry_mapping.suggest_mappings(settings, template)
    save_mapping(settings, mapping)
    return mapping


def approve_field(
    settings: DnaSettings, *, entity_id: str, field_id: str, silver_column: str | None = None
) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    industry_mapping.approve_field(mapping, entity_id, field_id, silver_column=silver_column or None)
    save_mapping(settings, mapping)
    return mapping


def reject_field(settings: DnaSettings, *, entity_id: str, field_id: str) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    industry_mapping.reject_field(mapping, entity_id, field_id)
    save_mapping(settings, mapping)
    return mapping


def approve_all(settings: DnaSettings, *, entity_id: str | None = None) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    industry_mapping.approve_all(mapping, entity_id=entity_id)
    save_mapping(settings, mapping)
    return mapping


def reject_all(settings: DnaSettings, *, entity_id: str | None = None) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    industry_mapping.reject_all(mapping, entity_id=entity_id)
    save_mapping(settings, mapping)
    return mapping


def exclude_entity(settings: DnaSettings, *, entity_id: str) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    template = load_template_for(mapping)
    industry_mapping.exclude_entity(mapping, template, entity_id)
    save_mapping(settings, mapping)
    return mapping


def exclude_field(settings: DnaSettings, *, entity_id: str, field_id: str) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    template = load_template_for(mapping)
    industry_mapping.exclude_field(mapping, template, entity_id, field_id)
    save_mapping(settings, mapping)
    return mapping


def add_custom_entity(
    settings: DnaSettings,
    *,
    entity_id: str,
    silver_source: str,
    silver_entity: str,
    field_id: str,
    silver_column: str,
) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    industry_mapping.add_custom_entity(
        mapping,
        entity_id=entity_id,
        silver_source=silver_source,
        silver_entity=silver_entity,
        fields=[(field_id, silver_column)],
    )
    save_mapping(settings, mapping)
    return mapping


def add_custom_field(
    settings: DnaSettings, *, entity_id: str, field_id: str, silver_column: str
) -> ClientModelMapping:
    mapping = _require_mapping(settings)
    industry_mapping.add_custom_field(mapping, entity_id, field_id=field_id, silver_column=silver_column)
    save_mapping(settings, mapping)
    return mapping


def completion(settings: DnaSettings) -> dict[str, Any]:
    mapping = _require_mapping(settings)
    template = load_template_for(mapping)
    return industry_mapping.mapping_completion(mapping, template)


def promote(settings: DnaSettings, *, version: str) -> dict[str, Any]:
    mapping = _require_mapping(settings)
    template = load_template_for(mapping)
    return industry_mapping.promote_mapping(settings, template, mapping, version=version)
