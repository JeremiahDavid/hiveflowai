"""Client mapping engine.

Binds a client's actual profiled silver data onto an industry template
(`hiveflow.dna.industry_templates`), tracks approval status and completion
per field, and materializes an approved mapping into a real `DefinitionPack`
so the existing `compile_pack` / `validate` / `publish` / `glue_runner`
pipeline runs unchanged.

Candidate suggestion prefers the data profiling engine's output
(`hiveflow.dna.data_profile` — purpose + per-field description, Bedrock-derived)
as extra matching signal over bare column names, but degrades gracefully to
`field_semantics` schema introspection for any table that hasn't been
profiled yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from hiveflow.compat import UTC
from typing import Any

from hiveflow.dna._text_match import norm, stems, tokens
from hiveflow.dna.industry_templates import IndustryEntitySpec, IndustryKpiSpec, IndustryTemplate
from hiveflow.dna.schema import (
    ApprovalRecord,
    CalendarSpec,
    DefinitionPack,
    EntitySpec,
    JoinSpec,
    KpiFormatSpec,
    KpiSpec,
    OutputSpec,
    PackStatus,
)
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import read_yaml_artifact, write_yaml_artifact
from hiveflow.storage.paths import governance_model_mapping_key

_ENTITY_MATCH_THRESHOLD = 0.3
_FIELD_MATCH_THRESHOLD = 0.4


class MappingStatus(str, Enum):
    UNMAPPED = "unmapped"
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class FieldMapping:
    field_id: str
    silver_column: str = ""
    status: str = MappingStatus.UNMAPPED.value
    confidence: float = 0.0
    reason: str = ""


@dataclass
class EntityMapping:
    entity_id: str
    included: bool = True
    silver_source: str = ""
    silver_entity: str = ""
    fields: list[FieldMapping] = field(default_factory=list)
    custom: bool = False

    def field_by_id(self, field_id: str) -> FieldMapping:
        for item in self.fields:
            if item.field_id == field_id:
                return item
        raise KeyError(f"Unknown field {field_id!r} on entity mapping {self.entity_id!r}")


@dataclass
class ClientModelMapping:
    industry_pack_id: str
    industry_version: str
    company: str
    entities: list[EntityMapping] = field(default_factory=list)
    excluded_entities: list[str] = field(default_factory=list)
    materialized_kpi_ids: list[str] = field(default_factory=list)
    updated_at: str = ""

    def entity_by_id(self, entity_id: str) -> EntityMapping:
        for item in self.entities:
            if item.entity_id == entity_id:
                return item
        raise KeyError(f"Unknown entity {entity_id!r} in mapping")

    def has_entity(self, entity_id: str) -> bool:
        return any(item.entity_id == entity_id for item in self.entities)


# ---------------------------------------------------------------------------
# Candidate generation (deterministic — no Bedrock in the mapping engine
# itself; it reads Bedrock-derived purpose/description text that the
# profiling engine already produced).
# ---------------------------------------------------------------------------


@dataclass
class _CandidateTable:
    source: str
    entity: str
    columns: list[str] = field(default_factory=list)
    purpose: str = ""
    field_descriptions: dict[str, str] = field(default_factory=dict)


def _gather_candidate_tables(settings: DnaSettings) -> list[_CandidateTable]:
    from hiveflow.dna.data_profile import load_data_profile_index, load_entity_profile
    from hiveflow.dna.field_semantics import discover_silver_stg_columns, list_lake_silver_stg_entities

    candidates: list[_CandidateTable] = []
    seen: set[tuple[str, str]] = set()

    index = load_data_profile_index(settings)
    for row in index.get("tables") or []:
        source = str(row.get("source") or "").strip()
        entity = str(row.get("entity") or "").strip()
        if not source or not entity:
            continue
        profile = load_entity_profile(settings, source, entity) or {}
        fields_payload = profile.get("fields") or []
        columns = [str(item.get("name") or "") for item in fields_payload if isinstance(item, dict)]
        descriptions = {
            str(item.get("name") or ""): str(item.get("description") or "")
            for item in fields_payload
            if isinstance(item, dict)
        }
        candidates.append(
            _CandidateTable(
                source=source,
                entity=entity,
                columns=[c for c in columns if c],
                purpose=str(profile.get("purpose") or row.get("purpose") or ""),
                field_descriptions=descriptions,
            )
        )
        seen.add((source, entity))

    for entity in list_lake_silver_stg_entities(settings):
        key = (settings.source, entity)
        if key in seen:
            continue
        candidates.append(
            _CandidateTable(
                source=settings.source,
                entity=entity,
                columns=discover_silver_stg_columns(settings, entity),
            )
        )
        seen.add(key)

    return candidates


def _text_score(name_tokens: set[str], name_stems: set[str], candidate_name: str) -> float:
    candidate_norm = norm(candidate_name)
    if not candidate_norm:
        return 0.0
    score = 0.0
    if candidate_norm in {norm(t) for t in name_tokens}:
        score = 0.9
    if name_stems & stems(candidate_name):
        score = max(score, 0.65)
    if name_tokens & tokens(candidate_name):
        score = max(score, 0.45)
    return score


def _entity_score(entity: IndustryEntitySpec, candidate: _CandidateTable) -> float:
    name_tokens = tokens(entity.id) | tokens(entity.display_name)
    name_stems = stems(entity.id) | stems(entity.display_name)
    score = _text_score(name_tokens, name_stems, candidate.entity)
    desc_overlap = (tokens(entity.description) | tokens(entity.grain)) & tokens(candidate.purpose)
    if desc_overlap:
        score = min(0.99, score + 0.05 * len(desc_overlap))
    return round(score, 2)


def _field_score(spec, candidate_column: str, candidate_description: str) -> float:
    name_tokens = tokens(spec.id) | tokens(spec.display_name)
    name_stems = stems(spec.id) | stems(spec.display_name)
    score = _text_score(name_tokens, name_stems, candidate_column)
    desc_overlap = tokens(spec.description) & tokens(candidate_description)
    if desc_overlap:
        score = min(0.99, score + 0.1 * len(desc_overlap))
    return round(score, 2)


def suggest_mappings(settings: DnaSettings, template: IndustryTemplate) -> ClientModelMapping:
    """Deterministic candidate mapping — prefers profiled purpose/description text,
    falls back to bare column introspection for unprofiled tables."""
    candidates = _gather_candidate_tables(settings)
    entity_mappings: list[EntityMapping] = []

    for entity in template.entities:
        best: _CandidateTable | None = None
        best_score = 0.0
        for candidate in candidates:
            score = _entity_score(entity, candidate)
            if score > best_score:
                best, best_score = candidate, score

        if best is None or best_score < _ENTITY_MATCH_THRESHOLD:
            entity_mappings.append(
                EntityMapping(entity_id=entity.id, fields=[FieldMapping(field_id=f.id) for f in entity.fields])
            )
            continue

        used_columns: set[str] = set()
        field_mappings: list[FieldMapping] = []
        for spec in entity.fields:
            best_column = ""
            best_field_score = 0.0
            for column in best.columns:
                if column in used_columns:
                    continue
                score = _field_score(spec, column, best.field_descriptions.get(column, ""))
                if score > best_field_score:
                    best_column, best_field_score = column, score
            if best_column and best_field_score >= _FIELD_MATCH_THRESHOLD:
                used_columns.add(best_column)
                field_mappings.append(
                    FieldMapping(
                        field_id=spec.id,
                        silver_column=best_column,
                        status=MappingStatus.SUGGESTED.value,
                        confidence=best_field_score,
                        reason=f"matched column {best_column!r}",
                    )
                )
            else:
                field_mappings.append(FieldMapping(field_id=spec.id))

        entity_mappings.append(
            EntityMapping(
                entity_id=entity.id,
                silver_source=best.source,
                silver_entity=best.entity,
                fields=field_mappings,
            )
        )

    return ClientModelMapping(
        industry_pack_id=template.pack_id,
        industry_version=template.version,
        company=settings.company,
        entities=entity_mappings,
        updated_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Persistence — single mutable document (no draft/validated/production
# ceremony; the *materialized* DefinitionPack gets full governance versioning
# for free via the existing workflow.save_definition_pack).
# ---------------------------------------------------------------------------


def _mapping_to_dict(mapping: ClientModelMapping) -> dict[str, Any]:
    return asdict(mapping)


def _mapping_from_dict(payload: dict[str, Any]) -> ClientModelMapping:
    entities = [
        EntityMapping(
            entity_id=str(item["entity_id"]),
            included=bool(item.get("included", True)),
            silver_source=str(item.get("silver_source") or ""),
            silver_entity=str(item.get("silver_entity") or ""),
            custom=bool(item.get("custom", False)),
            fields=[
                FieldMapping(
                    field_id=str(f["field_id"]),
                    silver_column=str(f.get("silver_column") or ""),
                    status=str(f.get("status") or MappingStatus.UNMAPPED.value),
                    confidence=float(f.get("confidence") or 0.0),
                    reason=str(f.get("reason") or ""),
                )
                for f in item.get("fields") or []
            ],
        )
        for item in payload.get("entities") or []
    ]
    return ClientModelMapping(
        industry_pack_id=str(payload["industry_pack_id"]),
        industry_version=str(payload.get("industry_version") or ""),
        company=str(payload.get("company") or ""),
        entities=entities,
        excluded_entities=list(payload.get("excluded_entities") or []),
        materialized_kpi_ids=list(payload.get("materialized_kpi_ids") or []),
        updated_at=str(payload.get("updated_at") or ""),
    )


def load_mapping(settings: DnaSettings) -> ClientModelMapping | None:
    payload = read_yaml_artifact(settings, governance_model_mapping_key(settings.dna_config_id))
    if not payload:
        return None
    return _mapping_from_dict(payload)


def save_mapping(settings: DnaSettings, mapping: ClientModelMapping) -> str:
    mapping.updated_at = datetime.now(UTC).isoformat()
    return write_yaml_artifact(
        settings, governance_model_mapping_key(settings.dna_config_id), _mapping_to_dict(mapping)
    )


def load_or_init_mapping(settings: DnaSettings, template: IndustryTemplate) -> ClientModelMapping:
    existing = load_mapping(settings)
    if existing is not None:
        return existing
    return suggest_mappings(settings, template)


# ---------------------------------------------------------------------------
# Approve / reject
# ---------------------------------------------------------------------------


def approve_field(
    mapping: ClientModelMapping, entity_id: str, field_id: str, *, silver_column: str | None = None
) -> ClientModelMapping:
    entity = mapping.entity_by_id(entity_id)
    fm = entity.field_by_id(field_id)
    if silver_column is not None:
        fm.silver_column = silver_column
    if not fm.silver_column:
        raise ValueError(f"Cannot approve {entity_id}.{field_id} without a silver_column")
    fm.status = MappingStatus.APPROVED.value
    return mapping


def reject_field(mapping: ClientModelMapping, entity_id: str, field_id: str, *, reason: str = "") -> ClientModelMapping:
    entity = mapping.entity_by_id(entity_id)
    fm = entity.field_by_id(field_id)
    fm.status = MappingStatus.REJECTED.value
    if reason:
        fm.reason = reason
    return mapping


def approve_all(mapping: ClientModelMapping, *, entity_id: str | None = None) -> ClientModelMapping:
    """Approve every suggested/mapped field, optionally scoped to one entity (a table)."""
    for entity in mapping.entities:
        if entity_id is not None and entity.entity_id != entity_id:
            continue
        for fm in entity.fields:
            if fm.silver_column:
                fm.status = MappingStatus.APPROVED.value
    return mapping


def reject_all(mapping: ClientModelMapping, *, entity_id: str | None = None) -> ClientModelMapping:
    """Reject every field, optionally scoped to one entity (a table)."""
    for entity in mapping.entities:
        if entity_id is not None and entity.entity_id != entity_id:
            continue
        for fm in entity.fields:
            fm.status = MappingStatus.REJECTED.value
    return mapping


# ---------------------------------------------------------------------------
# Add / remove (client schema customization)
# ---------------------------------------------------------------------------


def add_custom_entity(
    mapping: ClientModelMapping,
    *,
    entity_id: str,
    silver_source: str,
    silver_entity: str,
    fields: list[tuple[str, str]],
) -> ClientModelMapping:
    """Add a client-specific entity outside the industry template, pre-approved."""
    if mapping.has_entity(entity_id):
        raise ValueError(f"Entity {entity_id!r} already exists in mapping")
    field_mappings = [
        FieldMapping(
            field_id=field_id,
            silver_column=column,
            status=MappingStatus.APPROVED.value,
            confidence=1.0,
            reason="manually added",
        )
        for field_id, column in fields
    ]
    mapping.entities.append(
        EntityMapping(
            entity_id=entity_id,
            silver_source=silver_source,
            silver_entity=silver_entity,
            fields=field_mappings,
            custom=True,
        )
    )
    return mapping


def add_custom_field(
    mapping: ClientModelMapping, entity_id: str, *, field_id: str, silver_column: str
) -> ClientModelMapping:
    """Add a client-specific field to an existing (template or custom) entity, pre-approved."""
    entity = mapping.entity_by_id(entity_id)
    if any(item.field_id == field_id for item in entity.fields):
        raise ValueError(f"Field {field_id!r} already exists on entity {entity_id!r}")
    entity.fields.append(
        FieldMapping(
            field_id=field_id,
            silver_column=silver_column,
            status=MappingStatus.APPROVED.value,
            confidence=1.0,
            reason="manually added",
        )
    )
    return mapping


def _blocking_kpis(
    template: IndustryTemplate, *, entity_id: str, field_id: str | None, materialized_kpi_ids: list[str]
) -> list[str]:
    prefix = f"{entity_id}." if field_id is None else f"{entity_id}.{field_id}"
    blocking: list[str] = []
    for kpi in template.kpis:
        if kpi.id not in materialized_kpi_ids:
            continue
        required = template.required_fields_for_kpi(kpi.id)
        hit = any(ref.startswith(prefix) for ref in required) if field_id is None else prefix in required
        if hit:
            blocking.append(kpi.name or kpi.id)
    return blocking


def exclude_field(
    mapping: ClientModelMapping, template: IndustryTemplate, entity_id: str, field_id: str
) -> ClientModelMapping:
    """Remove a field from the client's model. Blocked (not auto-disabled) if an
    already-published KPI depends on it."""
    blocking = _blocking_kpis(
        template, entity_id=entity_id, field_id=field_id, materialized_kpi_ids=mapping.materialized_kpi_ids
    )
    if blocking:
        raise ValueError(
            f"Cannot remove {entity_id}.{field_id}: required by published KPI(s) {', '.join(blocking)}"
        )
    entity = mapping.entity_by_id(entity_id)
    entity.fields = [item for item in entity.fields if item.field_id != field_id]
    return mapping


def exclude_entity(mapping: ClientModelMapping, template: IndustryTemplate, entity_id: str) -> ClientModelMapping:
    """Remove an entity from the client's model. Blocked (not auto-disabled) if an
    already-published KPI depends on any of its fields."""
    blocking = _blocking_kpis(
        template, entity_id=entity_id, field_id=None, materialized_kpi_ids=mapping.materialized_kpi_ids
    )
    if blocking:
        raise ValueError(f"Cannot remove entity {entity_id!r}: required by published KPI(s) {', '.join(blocking)}")
    entity = mapping.entity_by_id(entity_id)
    entity.included = False
    if entity_id not in mapping.excluded_entities:
        mapping.excluded_entities.append(entity_id)
    return mapping


# ---------------------------------------------------------------------------
# Completion tracking
# ---------------------------------------------------------------------------


def mapping_completion(mapping: ClientModelMapping, template: IndustryTemplate) -> dict[str, Any]:
    """% complete overall and per entity. Required = template field marked
    `required` or referenced by any KPI's required_fields. Only `approved`
    status counts — matches the "block, don't auto-disable" removal rule:
    completion should reflect real, reviewed bindings, not mere suggestions."""
    required_refs: set[str] = set()
    for entity in template.entities:
        for item in entity.fields:
            if item.required:
                required_refs.add(f"{entity.id}.{item.id}")
    for kpi in template.kpis:
        required_refs.update(template.required_fields_for_kpi(kpi.id))

    approved_refs: set[str] = set()
    for entity in mapping.entities:
        if not entity.included:
            continue
        for item in entity.fields:
            if item.status == MappingStatus.APPROVED.value:
                approved_refs.add(f"{entity.entity_id}.{item.field_id}")

    by_entity: dict[str, dict[str, Any]] = {}
    for entity in template.entities:
        entity_required = {ref for ref in required_refs if ref.startswith(f"{entity.id}.")}
        entity_approved = entity_required & approved_refs
        total = len(entity_required)
        done = len(entity_approved)
        by_entity[entity.id] = {
            "required": total,
            "approved": done,
            "percent": round(100.0 * done / total, 1) if total else 100.0,
        }

    total_required = len(required_refs)
    total_approved = len(required_refs & approved_refs)
    return {
        "overall_percent": round(100.0 * total_approved / total_required, 1) if total_required else 100.0,
        "required": total_required,
        "approved": total_approved,
        "by_entity": by_entity,
    }


# ---------------------------------------------------------------------------
# Materialization — the seam into the existing governance/compile pipeline.
# ---------------------------------------------------------------------------


def materialize_to_definition_pack(
    template: IndustryTemplate,
    mapping: ClientModelMapping,
    *,
    pack_id: str,
    version: str,
) -> tuple[DefinitionPack, dict[str, Any]]:
    """Project an approved mapping into a real DefinitionPack. Pure function —
    no I/O. Entities/joins materialize 1:1 from approved, included mappings.
    KPIs: auto_materializable ones with every required field approved become
    real KpiSpecs; everything else is reported with a reason, never silently
    dropped."""
    report: dict[str, Any] = {
        "included_entities": [],
        "excluded_entities": [],
        "included_kpis": [],
        "skipped_kpis": [],
    }

    entity_specs: list[EntitySpec] = []
    field_binding: dict[str, str] = {}
    included_entity_ids: set[str] = set()

    for entity_mapping in mapping.entities:
        if entity_mapping.entity_id in mapping.excluded_entities or not entity_mapping.included:
            report["excluded_entities"].append({"entity_id": entity_mapping.entity_id, "reason": "excluded by client"})
            continue
        if not entity_mapping.silver_entity:
            report["excluded_entities"].append({"entity_id": entity_mapping.entity_id, "reason": "not mapped to a silver table"})
            continue

        template_entity: IndustryEntitySpec | None
        try:
            template_entity = template.entity_by_id(entity_mapping.entity_id)
        except KeyError:
            template_entity = None

        approved_columns: dict[str, str] = {}
        for fm in entity_mapping.fields:
            if fm.status == MappingStatus.APPROVED.value and fm.silver_column:
                approved_columns[fm.field_id] = fm.silver_column
                field_binding[f"{entity_mapping.entity_id}.{fm.field_id}"] = fm.silver_column

        pk_column = ""
        if template_entity is not None:
            pk_column = approved_columns.get(template_entity.primary_key, "")
        if not pk_column:
            pk_column = next(iter(approved_columns.values()), "id")

        entity_specs.append(
            EntitySpec(
                id=entity_mapping.entity_id,
                grain=template_entity.grain if template_entity else "",
                silver_entity=entity_mapping.silver_entity,
                primary_key=pk_column,
                description=template_entity.description if template_entity else "",
            )
        )
        included_entity_ids.add(entity_mapping.entity_id)
        report["included_entities"].append(entity_mapping.entity_id)

    join_specs: list[JoinSpec] = []
    for join in template.joins:
        if join.left_entity in included_entity_ids and join.right_entity in included_entity_ids:
            left_key = field_binding.get(f"{join.left_entity}.{join.left_key}", join.left_key)
            right_key = field_binding.get(f"{join.right_entity}.{join.right_key}", join.right_key)
            join_specs.append(
                JoinSpec(
                    id=join.id,
                    left_entity=join.left_entity,
                    right_entity=join.right_entity,
                    left_key=left_key,
                    right_key=right_key,
                    cardinality=join.cardinality,
                    description=join.description,
                )
            )

    def _required_fields_approved(kpi: IndustryKpiSpec) -> bool:
        for ref in template.required_fields_for_kpi(kpi.id):
            entity_id = ref.rsplit(".", 1)[0]
            if entity_id not in included_entity_ids or ref not in field_binding:
                return False
        return True

    kpi_specs: list[KpiSpec] = []
    for kpi in template.kpis:
        if not kpi.auto_materializable:
            report["skipped_kpis"].append({"kpi_id": kpi.id, "reason": "auto_materializable is false — author via KPI Generator"})
            continue
        if kpi.entity_id not in included_entity_ids:
            report["skipped_kpis"].append({"kpi_id": kpi.id, "reason": f"entity {kpi.entity_id!r} not included"})
            continue
        if not _required_fields_approved(kpi):
            report["skipped_kpis"].append({"kpi_id": kpi.id, "reason": "required fields not fully approved"})
            continue

        value_column = field_binding.get(f"{kpi.entity_id}.{kpi.value_field}", "") if kpi.value_field else ""
        filter_column = field_binding.get(f"{kpi.entity_id}.{kpi.filter_field}", "") if kpi.filter_field else ""
        group_by = [field_binding[f"{kpi.entity_id}.{g}"] for g in kpi.group_by]

        kpi_specs.append(
            KpiSpec(
                id=kpi.id,
                name=kpi.name,
                definition=kpi.description,
                formula_type=kpi.formula_type,
                source_output=kpi.entity_id,
                value_column=value_column,
                filter_column=filter_column,
                filter_value=kpi.filter_value,
                group_by=group_by,
                numerator_kpi=kpi.numerator_kpi,
                denominator_kpi=kpi.denominator_kpi,
                base_kpi=kpi.base_kpi,
                compare=kpi.compare,
                format=KpiFormatSpec(**kpi.format) if kpi.format else None,
            )
        )
        report["included_kpis"].append(kpi.id)

    # One kpi_aggregate OutputSpec per entity, listing only "public" KPIs — a
    # KPI referenced as someone else's numerator_kpi/denominator_kpi/base_kpi
    # is a helper component: it must exist in pack.kpis for kpi_by_id to
    # resolve it, but doesn't need its own snapshot row.
    sibling_refs: set[str] = set()
    for kpi in template.kpis:
        for ref in (kpi.numerator_kpi, kpi.denominator_kpi, kpi.base_kpi):
            if ref:
                sibling_refs.add(ref)

    output_specs: list[OutputSpec] = []
    kpis_by_entity: dict[str, list[str]] = {}
    for kpi_id in report["included_kpis"]:
        if kpi_id in sibling_refs:
            continue
        entity_id = template.kpi_by_id(kpi_id).entity_id
        kpis_by_entity.setdefault(entity_id, []).append(kpi_id)
    for entity_id, kpi_ids in kpis_by_entity.items():
        output_specs.append(
            OutputSpec(
                id=f"{entity_id}_kpis",
                output_type="kpi_snapshot",
                build="kpi_aggregate",
                entity_id=entity_id,
                kpi_ids=kpi_ids,
            )
        )

    calendar_spec: CalendarSpec | None = None
    if template.calendar is not None:
        cal_entity, cal_field = template.calendar.date_field.split(".", 1)
        date_column = field_binding.get(f"{cal_entity}.{cal_field}", "")
        if cal_entity in included_entity_ids and date_column:
            calendar_spec = CalendarSpec(
                id=template.calendar.id,
                type=template.calendar.type,
                date_column=date_column,
                period_grain=template.calendar.period_grain,
                fiscal_year_start_month=template.calendar.fiscal_year_start_month,
                description=template.calendar.description,
            )

    pack = DefinitionPack(
        pack_id=pack_id,
        version=version,
        status=PackStatus.DRAFT.value,
        source_system=mapping.entities[0].silver_source if mapping.entities else "",
        entities=entity_specs,
        joins=join_specs,
        outputs=output_specs,
        kpis=kpi_specs,
        tests=[],
        approval=ApprovalRecord(status=PackStatus.DRAFT.value),
        description=f"Materialized from industry template {template.pack_id!r} v{template.version}.",
        calendar=calendar_spec,
    )
    return pack, report


def promote_mapping(
    settings: DnaSettings,
    template: IndustryTemplate,
    mapping: ClientModelMapping,
    *,
    version: str,
) -> dict[str, Any]:
    """Materialize the mapping and write it through the existing governance
    pipeline (`workflow.save_definition_pack`) as a new draft pack version.
    Updates `materialized_kpi_ids` so future removals are correctly blocked."""
    from hiveflow.dna.workflow import save_definition_pack

    pack_id = settings.dna_config_id
    pack, report = materialize_to_definition_pack(template, mapping, pack_id=pack_id, version=version)
    pack_path = save_definition_pack(settings, pack)

    for kpi_id in report["included_kpis"]:
        if kpi_id not in mapping.materialized_kpi_ids:
            mapping.materialized_kpi_ids.append(kpi_id)
    mapping_path = save_mapping(settings, mapping)

    return {"pack_path": pack_path, "mapping_path": mapping_path, "report": report, "pack": pack}
