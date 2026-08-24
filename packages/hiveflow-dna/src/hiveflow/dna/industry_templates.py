"""Industry data model templates.

A template is the canonical entities/fields/joins/KPI catalog for one
industry — source-agnostic, no `silver_entity` binding. Each industry is its
own foundation; there is no shared "canonical core" across industries.

A client mapping (`hiveflow.dna.industry_mapping`) binds a client's actual
silver data onto a template; materialization projects the approved mapping
into a real `DefinitionPack` (`hiveflow.dna.schema`), so `compile_pack` /
`validate` / `publish` need no changes to consume it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

_MAX_SCHEMA_ERRORS = 5
_VALUE_FIELD_REQUIRED = {"sum", "count_distinct", "avg"}


def industry_template_schema_path() -> Path:
    return Path(__file__).resolve().parent / "schema" / "industry-template.schema.json"


def industry_pack_dir() -> Path:
    return Path(__file__).resolve().parent / "packs" / "industry"


@lru_cache(maxsize=1)
def _industry_template_validator() -> Draft202012Validator:
    schema_path = industry_template_schema_path()
    if not schema_path.is_file():
        raise FileNotFoundError(f"Industry template schema not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid industry-template.schema.json: {exc.message}") from exc
    return Draft202012Validator(schema)


def _format_schema_path(error: Any) -> str:
    parts = [str(part) for part in error.absolute_path]
    return ".".join(parts) if parts else "(root)"


def validate_industry_template_schema(payload: dict[str, Any]) -> None:
    """Validate against industry-template.schema.json (Draft 2020-12)."""
    if not isinstance(payload, dict):
        raise ValueError("Industry template must be a mapping")

    validator = _industry_template_validator()
    errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.absolute_path))
    if not errors:
        return
    messages = [f"{_format_schema_path(err)}: {err.message}" for err in errors[:_MAX_SCHEMA_ERRORS]]
    remaining = len(errors) - len(messages)
    if remaining > 0:
        messages.append(f"... and {remaining} more error(s)")
    raise ValueError("Industry template schema validation failed:\n" + "\n".join(messages))


@dataclass
class IndustryFieldSpec:
    id: str
    display_name: str = ""
    type: str = "string"
    required: bool = False
    description: str = ""


@dataclass
class IndustryEntitySpec:
    id: str
    grain: str
    primary_key: str
    fields: list[IndustryFieldSpec] = field(default_factory=list)
    display_name: str = ""
    description: str = ""
    required: bool = False

    def field_by_id(self, field_id: str) -> IndustryFieldSpec:
        for item in self.fields:
            if item.id == field_id:
                return item
        raise KeyError(f"Unknown field {field_id!r} on entity {self.id!r}")


@dataclass
class IndustryJoinSpec:
    id: str
    left_entity: str
    right_entity: str
    left_key: str
    right_key: str
    cardinality: str
    description: str = ""


@dataclass
class IndustryCalendarSpec:
    id: str
    type: str
    date_field: str
    period_grain: str = "month"
    fiscal_year_start_month: int = 1
    description: str = ""


@dataclass
class IndustryKpiSpec:
    id: str
    name: str
    entity_id: str
    formula_type: str
    description: str = ""
    value_field: str = ""
    filter_field: str = ""
    filter_value: Any = None
    group_by: list[str] = field(default_factory=list)
    numerator_kpi: str = ""
    denominator_kpi: str = ""
    base_kpi: str = ""
    compare: str = ""
    auto_materializable: bool = True
    format: dict[str, Any] | None = None
    doc_citation: str = ""

    def own_field_refs(self) -> list[str]:
        """entity_id.field_id refs this KPI itself needs (not resolving siblings)."""
        refs: list[str] = []
        if self.value_field:
            refs.append(f"{self.entity_id}.{self.value_field}")
        if self.filter_field:
            refs.append(f"{self.entity_id}.{self.filter_field}")
        for group_field in self.group_by:
            refs.append(f"{self.entity_id}.{group_field}")
        return refs


@dataclass
class IndustryTemplate:
    pack_id: str
    version: str
    status: str
    entities: list[IndustryEntitySpec] = field(default_factory=list)
    joins: list[IndustryJoinSpec] = field(default_factory=list)
    kpis: list[IndustryKpiSpec] = field(default_factory=list)
    calendar: IndustryCalendarSpec | None = None
    description: str = ""

    def entity_by_id(self, entity_id: str) -> IndustryEntitySpec:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        raise KeyError(f"Unknown entity {entity_id!r}")

    def kpi_by_id(self, kpi_id: str) -> IndustryKpiSpec:
        for kpi in self.kpis:
            if kpi.id == kpi_id:
                return kpi
        raise KeyError(f"Unknown KPI {kpi_id!r}")

    def required_fields_for_kpi(self, kpi_id: str, *, _seen: frozenset[str] = frozenset()) -> list[str]:
        """entity_id.field_id refs a KPI needs, resolved transitively through
        numerator_kpi/denominator_kpi/base_kpi for ratio/period_compare KPIs."""
        if kpi_id in _seen:
            raise ValueError(f"Cycle detected in KPI dependencies at {kpi_id!r}")
        kpi = self.kpi_by_id(kpi_id)
        refs = list(kpi.own_field_refs())
        seen = _seen | {kpi_id}
        for sibling_id in (kpi.numerator_kpi, kpi.denominator_kpi, kpi.base_kpi):
            if sibling_id:
                refs.extend(self.required_fields_for_kpi(sibling_id, _seen=seen))
        deduped: list[str] = []
        for ref in refs:
            if ref not in deduped:
                deduped.append(ref)
        return deduped


def _load_field(payload: dict[str, Any]) -> IndustryFieldSpec:
    return IndustryFieldSpec(
        id=str(payload["id"]),
        display_name=str(payload.get("display_name") or ""),
        type=str(payload.get("type") or "string"),
        required=bool(payload.get("required", False)),
        description=str(payload.get("description") or ""),
    )


def _load_entity(payload: dict[str, Any]) -> IndustryEntitySpec:
    return IndustryEntitySpec(
        id=str(payload["id"]),
        grain=str(payload["grain"]),
        primary_key=str(payload["primary_key"]),
        fields=[_load_field(item) for item in payload.get("fields") or []],
        display_name=str(payload.get("display_name") or ""),
        description=str(payload.get("description") or ""),
        required=bool(payload.get("required", False)),
    )


def _load_join(payload: dict[str, Any]) -> IndustryJoinSpec:
    return IndustryJoinSpec(
        id=str(payload["id"]),
        left_entity=str(payload["left_entity"]),
        right_entity=str(payload["right_entity"]),
        left_key=str(payload["left_key"]),
        right_key=str(payload["right_key"]),
        cardinality=str(payload["cardinality"]),
        description=str(payload.get("description") or ""),
    )


def _load_calendar(payload: dict[str, Any] | None) -> IndustryCalendarSpec | None:
    if not payload:
        return None
    return IndustryCalendarSpec(
        id=str(payload["id"]),
        type=str(payload["type"]),
        date_field=str(payload["date_field"]),
        period_grain=str(payload.get("period_grain") or "month"),
        fiscal_year_start_month=int(payload.get("fiscal_year_start_month") or 1),
        description=str(payload.get("description") or ""),
    )


def _load_kpi(payload: dict[str, Any]) -> IndustryKpiSpec:
    return IndustryKpiSpec(
        id=str(payload["id"]),
        name=str(payload["name"]),
        entity_id=str(payload["entity_id"]),
        formula_type=str(payload["formula_type"]),
        description=str(payload.get("description") or ""),
        value_field=str(payload.get("value_field") or ""),
        filter_field=str(payload.get("filter_field") or ""),
        filter_value=payload.get("filter_value"),
        group_by=list(payload.get("group_by") or []),
        numerator_kpi=str(payload.get("numerator_kpi") or ""),
        denominator_kpi=str(payload.get("denominator_kpi") or ""),
        base_kpi=str(payload.get("base_kpi") or ""),
        compare=str(payload.get("compare") or ""),
        auto_materializable=bool(payload.get("auto_materializable", True)),
        format=payload.get("format"),
        doc_citation=str(payload.get("doc_citation") or ""),
    )


def _validate_references(template: IndustryTemplate) -> None:
    entity_ids = {entity.id for entity in template.entities}
    errors: list[str] = []

    for entity in template.entities:
        field_ids = {item.id for item in entity.fields}
        if entity.primary_key not in field_ids:
            errors.append(
                f"entity {entity.id!r}: primary_key {entity.primary_key!r} is not a field on this entity"
            )

    for join in template.joins:
        if join.left_entity not in entity_ids:
            errors.append(f"join {join.id!r}: unknown left_entity {join.left_entity!r}")
        if join.right_entity not in entity_ids:
            errors.append(f"join {join.id!r}: unknown right_entity {join.right_entity!r}")

    kpi_ids = {kpi.id for kpi in template.kpis}
    for kpi in template.kpis:
        if kpi.entity_id not in entity_ids:
            errors.append(f"kpi {kpi.id!r}: unknown entity_id {kpi.entity_id!r}")
            continue
        entity = template.entity_by_id(kpi.entity_id)
        field_ids = {item.id for item in entity.fields}
        for value, label in ((kpi.value_field, "value_field"), (kpi.filter_field, "filter_field")):
            if value and value not in field_ids:
                errors.append(f"kpi {kpi.id!r}: unknown {label} {value!r} on entity {kpi.entity_id!r}")
        for group_field in kpi.group_by:
            if group_field not in field_ids:
                errors.append(
                    f"kpi {kpi.id!r}: unknown group_by field {group_field!r} on entity {kpi.entity_id!r}"
                )
        for sibling, label in (
            (kpi.numerator_kpi, "numerator_kpi"),
            (kpi.denominator_kpi, "denominator_kpi"),
            (kpi.base_kpi, "base_kpi"),
        ):
            if sibling and sibling not in kpi_ids:
                errors.append(f"kpi {kpi.id!r}: unknown {label} {sibling!r}")
        if kpi.formula_type == "ratio" and not (kpi.numerator_kpi and kpi.denominator_kpi):
            errors.append(f"kpi {kpi.id!r}: formula_type=ratio requires numerator_kpi and denominator_kpi")
        if kpi.formula_type == "period_compare" and not kpi.base_kpi:
            errors.append(f"kpi {kpi.id!r}: formula_type=period_compare requires base_kpi")
        if kpi.formula_type in _VALUE_FIELD_REQUIRED and not kpi.value_field:
            errors.append(f"kpi {kpi.id!r}: formula_type={kpi.formula_type!r} requires value_field")

    if template.calendar is not None:
        if "." not in template.calendar.date_field:
            errors.append(f"calendar date_field {template.calendar.date_field!r} must be entity_id.field_id")
        else:
            entity_id, field_id = template.calendar.date_field.split(".", 1)
            if entity_id not in entity_ids:
                errors.append(f"calendar date_field: unknown entity {entity_id!r}")
            elif field_id not in {item.id for item in template.entity_by_id(entity_id).fields}:
                errors.append(f"calendar date_field: unknown field {field_id!r} on entity {entity_id!r}")

    for kpi in template.kpis:
        try:
            template.required_fields_for_kpi(kpi.id)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise ValueError("Industry template reference validation failed:\n" + "\n".join(errors))


def load_industry_template(payload: dict[str, Any]) -> IndustryTemplate:
    validate_industry_template_schema(payload)
    template = IndustryTemplate(
        pack_id=str(payload["pack_id"]),
        version=str(payload["version"]),
        status=str(payload["status"]),
        description=str(payload.get("description") or ""),
        entities=[_load_entity(item) for item in payload.get("entities") or []],
        joins=[_load_join(item) for item in payload.get("joins") or []],
        kpis=[_load_kpi(item) for item in payload.get("kpis") or []],
        calendar=_load_calendar(payload.get("calendar")),
    )
    _validate_references(template)
    return template


def load_industry_template_yaml(text: str) -> IndustryTemplate:
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("Industry template YAML must be a mapping at the top level")
    return load_industry_template(payload)


def load_industry_template_file(path: str | Path) -> IndustryTemplate:
    return load_industry_template_yaml(Path(path).read_text(encoding="utf-8"))


def list_available_industry_templates() -> list[str]:
    """Pack ids of industry templates shipped in packs/industry/."""
    directory = industry_pack_dir()
    if not directory.is_dir():
        return []
    return sorted({path.stem.rsplit("_v", 1)[0] for path in directory.glob("*_v*.yaml")})


def load_industry_template_by_id(pack_id: str) -> IndustryTemplate:
    directory = industry_pack_dir()
    matches = sorted(directory.glob(f"{pack_id.strip().lower()}_v*.yaml"))
    if not matches:
        raise FileNotFoundError(f"No industry template found for pack_id={pack_id!r} in {directory}")
    return load_industry_template_file(matches[-1])
