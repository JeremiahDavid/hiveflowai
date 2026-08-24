"""Tests for the client mapping engine (suggest, approve/reject, % complete,
add/remove, and materialization into a real, compilable DefinitionPack)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveflow.dna.compile import compile_pack
from hiveflow.dna.industry_mapping import (
    ClientModelMapping,
    EntityMapping,
    FieldMapping,
    MappingStatus,
    add_custom_entity,
    add_custom_field,
    approve_all,
    approve_field,
    exclude_entity,
    exclude_field,
    load_mapping,
    mapping_completion,
    materialize_to_definition_pack,
    promote_mapping,
    reject_all,
    reject_field,
    save_mapping,
    suggest_mappings,
)
from hiveflow.dna.industry_templates import load_industry_template
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import read_staging_output
from hiveflow.storage.parquet import write_parquet_local
from hiveflow.storage.paths import prefix_path, silver_entity_prefix, silver_stg_entity_prefix

_ORDER_TEMPLATE_PAYLOAD = {
    "pack_id": "widgets",
    "version": "1.0.0",
    "status": "draft",
    "entities": [
        {
            "id": "order",
            "grain": "one row per order",
            "primary_key": "order_key",
            "description": "Sales order header",
            "fields": [
                {"id": "order_key", "required": True},
                {"id": "amount", "type": "currency", "required": True},
                {"id": "status", "type": "string"},
            ],
        }
    ],
    "kpis": [
        {
            "id": "order_total",
            "name": "Order Total",
            "entity_id": "order",
            "formula_type": "sum",
            "value_field": "amount",
        },
        {
            "id": "accept_rate",
            "name": "Accept Rate",
            "entity_id": "order",
            "formula_type": "ratio",
            "numerator_kpi": "accepted_count",
            "denominator_kpi": "total_count",
        },
        {
            "id": "accepted_count",
            "name": "Accepted Count",
            "entity_id": "order",
            "formula_type": "count",
            "filter_field": "status",
            "filter_value": "accepted",
        },
        {
            "id": "total_count",
            "name": "Total Count",
            "entity_id": "order",
            "formula_type": "count",
        },
    ],
}


@pytest.fixture
def template():
    return load_industry_template(_ORDER_TEMPLATE_PAYLOAD)


@pytest.fixture
def settings(tmp_path: Path) -> DnaSettings:
    return DnaSettings(source="dbc", data_dir=tmp_path, company="POC")


def _write_silver_stg(settings: DnaSettings, entity: str, rows: list[dict]) -> None:
    out = prefix_path(settings.data_dir, silver_stg_entity_prefix(settings.source, entity))
    write_parquet_local(out, "data.parquet", rows)


def _write_silver(settings: DnaSettings, source: str, entity: str, rows: list[dict]) -> None:
    out = prefix_path(settings.data_dir, silver_entity_prefix(source, entity))
    write_parquet_local(out, "data.parquet", rows)


# ---------------------------------------------------------------------------
# suggest_mappings
# ---------------------------------------------------------------------------


def test_suggest_mappings_matches_by_name_and_stem_no_profile(settings, template) -> None:
    _write_silver_stg(
        settings,
        "order",
        [
            {"order_key": "o1", "amount": 100, "status": "accepted"},
            {"order_key": "o2", "amount": 50, "status": "declined"},
        ],
    )

    mapping = suggest_mappings(settings, template)
    entity = mapping.entity_by_id("order")
    assert entity.silver_source == "dbc"
    assert entity.silver_entity == "order"

    by_field = {f.field_id: f for f in entity.fields}
    assert by_field["order_key"].silver_column == "order_key"
    assert by_field["order_key"].status == MappingStatus.SUGGESTED.value
    assert by_field["amount"].silver_column == "amount"
    assert by_field["status"].silver_column == "status"


def test_suggest_mappings_leaves_entity_unmapped_when_nothing_matches(settings, template) -> None:
    _write_silver_stg(settings, "completely_unrelated_table", [{"x": 1}])
    mapping = suggest_mappings(settings, template)
    entity = mapping.entity_by_id("order")
    assert entity.silver_entity == ""
    assert all(f.status == MappingStatus.UNMAPPED.value for f in entity.fields)


def test_suggest_mappings_prefers_profiled_purpose_over_unprofiled_name_match(settings, template) -> None:
    # "order_hdr" matches by name token ("order") but is never profiled.
    _write_silver_stg(settings, "order_hdr", [{"id": "1"}])
    # "order_bak" matches the same name token, AND is profiled with a purpose
    # that strongly overlaps the template entity's description/grain — it
    # should win despite the less on-the-nose name.
    _write_silver_stg(settings, "order_bak", [{"id": "1"}])

    from hiveflow.dna.data_profile import profile_entity

    def _stub_invoke(_system: str, _user: str) -> str:
        return json.dumps(
            {
                "purpose": "Sales order header backup table",
                "confidence": 0.8,
                "fields": [{"name": "id", "description": "Row id"}],
            }
        )

    profile_entity(settings, "dbc", "order_bak", invoke=_stub_invoke)

    mapping = suggest_mappings(settings, template)
    entity = mapping.entity_by_id("order")
    assert entity.silver_entity == "order_bak"


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


def _fresh_mapping() -> ClientModelMapping:
    return ClientModelMapping(
        industry_pack_id="widgets",
        industry_version="1.0.0",
        company="POC",
        entities=[
            EntityMapping(
                entity_id="order",
                silver_source="dbc",
                silver_entity="order",
                fields=[
                    FieldMapping(field_id="order_key", silver_column="order_key", status=MappingStatus.SUGGESTED.value, confidence=0.9),
                    FieldMapping(field_id="amount", silver_column="amount", status=MappingStatus.SUGGESTED.value, confidence=0.9),
                    FieldMapping(field_id="status"),
                ],
            )
        ],
    )


def test_approve_field_requires_a_silver_column() -> None:
    mapping = _fresh_mapping()
    with pytest.raises(ValueError, match="without a silver_column"):
        approve_field(mapping, "order", "status")


def test_approve_field_marks_approved() -> None:
    mapping = _fresh_mapping()
    approve_field(mapping, "order", "order_key")
    assert mapping.entity_by_id("order").field_by_id("order_key").status == MappingStatus.APPROVED.value


def test_reject_field_marks_rejected_with_reason() -> None:
    mapping = _fresh_mapping()
    reject_field(mapping, "order", "amount", reason="wrong column")
    fm = mapping.entity_by_id("order").field_by_id("amount")
    assert fm.status == MappingStatus.REJECTED.value
    assert fm.reason == "wrong column"


def test_approve_all_scoped_to_entity() -> None:
    mapping = _fresh_mapping()
    approve_all(mapping, entity_id="order")
    entity = mapping.entity_by_id("order")
    assert entity.field_by_id("order_key").status == MappingStatus.APPROVED.value
    assert entity.field_by_id("amount").status == MappingStatus.APPROVED.value
    # "status" field has no silver_column suggested — approve_all only approves mapped fields.
    assert entity.field_by_id("status").status == MappingStatus.UNMAPPED.value


def test_reject_all_rejects_every_field_regardless_of_mapping() -> None:
    mapping = _fresh_mapping()
    reject_all(mapping)
    entity = mapping.entity_by_id("order")
    assert all(f.status == MappingStatus.REJECTED.value for f in entity.fields)


# ---------------------------------------------------------------------------
# % complete
# ---------------------------------------------------------------------------


def test_mapping_completion_counts_only_approved(template) -> None:
    mapping = _fresh_mapping()
    completion = mapping_completion(mapping, template)
    # required_refs = order.order_key (field required) + order.amount (KPI required_fields) + order.status (KPI required_fields)
    assert completion["required"] == 3
    assert completion["approved"] == 0
    assert completion["overall_percent"] == 0.0

    approve_field(mapping, "order", "order_key")
    approve_field(mapping, "order", "amount")
    completion = mapping_completion(mapping, template)
    assert completion["approved"] == 2
    assert completion["overall_percent"] == pytest.approx(66.7, abs=0.1)
    assert completion["by_entity"]["order"]["approved"] == 2


def test_mapping_completion_excludes_unincluded_entities(template) -> None:
    mapping = _fresh_mapping()
    approve_all(mapping)
    mapping.entity_by_id("order").included = False
    completion = mapping_completion(mapping, template)
    assert completion["approved"] == 0


# ---------------------------------------------------------------------------
# add / remove (schema customization)
# ---------------------------------------------------------------------------


def test_add_custom_entity_and_field() -> None:
    mapping = _fresh_mapping()
    add_custom_entity(
        mapping,
        entity_id="warranty_claim",
        silver_source="dbc",
        silver_entity="warranty_claims",
        fields=[("claim_id", "claim_id"), ("amount", "claim_amount")],
    )
    entity = mapping.entity_by_id("warranty_claim")
    assert entity.custom is True
    assert entity.field_by_id("claim_id").status == MappingStatus.APPROVED.value

    add_custom_field(mapping, "order", field_id="notes", silver_column="notes_txt")
    assert mapping.entity_by_id("order").field_by_id("notes").silver_column == "notes_txt"


def test_add_custom_entity_rejects_duplicate() -> None:
    mapping = _fresh_mapping()
    with pytest.raises(ValueError, match="already exists"):
        add_custom_entity(mapping, entity_id="order", silver_source="dbc", silver_entity="x", fields=[])


def test_exclude_field_allowed_when_no_published_kpi_depends_on_it(template) -> None:
    mapping = _fresh_mapping()
    exclude_field(mapping, template, "order", "status")
    assert not any(f.field_id == "status" for f in mapping.entity_by_id("order").fields)


def test_exclude_field_blocked_when_published_kpi_depends_on_it(template) -> None:
    mapping = _fresh_mapping()
    mapping.materialized_kpi_ids = ["accept_rate"]
    with pytest.raises(ValueError, match="Accept Rate"):
        exclude_field(mapping, template, "order", "status")


def test_exclude_entity_blocked_when_published_kpi_depends_on_it(template) -> None:
    mapping = _fresh_mapping()
    mapping.materialized_kpi_ids = ["order_total"]
    with pytest.raises(ValueError, match="Order Total"):
        exclude_entity(mapping, template, "order")


def test_exclude_entity_allowed_when_unpublished(template) -> None:
    mapping = _fresh_mapping()
    exclude_entity(mapping, template, "order")
    assert mapping.entity_by_id("order").included is False
    assert "order" in mapping.excluded_entities


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_save_and_load_mapping_round_trip(settings) -> None:
    mapping = _fresh_mapping()
    save_mapping(settings, mapping)
    loaded = load_mapping(settings)
    assert loaded is not None
    assert loaded.industry_pack_id == "widgets"
    assert loaded.entity_by_id("order").silver_entity == "order"


def test_load_mapping_returns_none_when_absent(settings) -> None:
    assert load_mapping(settings) is None


# ---------------------------------------------------------------------------
# materialize_to_definition_pack + real compile_pack integration
# ---------------------------------------------------------------------------


def _fully_approved_mapping() -> ClientModelMapping:
    return ClientModelMapping(
        industry_pack_id="widgets",
        industry_version="1.0.0",
        company="POC",
        entities=[
            EntityMapping(
                entity_id="order",
                silver_source="dbc",
                silver_entity="orders",
                fields=[
                    FieldMapping(field_id="order_key", silver_column="order_id", status=MappingStatus.APPROVED.value),
                    FieldMapping(field_id="amount", silver_column="amt_value", status=MappingStatus.APPROVED.value),
                    FieldMapping(field_id="status", silver_column="stat_value", status=MappingStatus.APPROVED.value),
                ],
            )
        ],
    )


def test_materialize_translates_canonical_refs_to_real_columns(template) -> None:
    mapping = _fully_approved_mapping()
    pack, report = materialize_to_definition_pack(template, mapping, pack_id="widgets_dna_config", version="1.0.0")

    assert report["included_entities"] == ["order"]
    assert set(report["included_kpis"]) == {"order_total", "accept_rate", "accepted_count", "total_count"}
    assert report["skipped_kpis"] == []

    entity_spec = pack.entity_by_id("order")
    assert entity_spec.silver_entity == "orders"
    assert entity_spec.primary_key == "order_id"

    order_total = pack.kpi_by_id("order_total")
    assert order_total.value_column == "amt_value"
    accepted_count = pack.kpi_by_id("accepted_count")
    assert accepted_count.filter_column == "stat_value"

    # Only "public" kpis (not referenced as someone else's numerator/denominator) get an output.
    assert len(pack.outputs) == 1
    assert set(pack.outputs[0].kpi_ids) == {"order_total", "accept_rate"}


def test_materialize_skips_kpi_with_unapproved_required_field(template) -> None:
    mapping = _fully_approved_mapping()
    mapping.entity_by_id("order").field_by_id("amount").status = MappingStatus.SUGGESTED.value
    _, report = materialize_to_definition_pack(template, mapping, pack_id="widgets_dna_config", version="1.0.0")
    assert "order_total" not in report["included_kpis"]
    reasons = {item["kpi_id"]: item["reason"] for item in report["skipped_kpis"]}
    assert "required fields not fully approved" in reasons["order_total"]


def test_materialized_pack_actually_compiles_and_computes_correct_values(settings, template) -> None:
    mapping = _fully_approved_mapping()
    pack, report = materialize_to_definition_pack(template, mapping, pack_id="widgets_dna_config", version="1.0.0")
    assert set(report["included_kpis"]) == {"order_total", "accept_rate", "accepted_count", "total_count"}

    # Simulate the approve step (materialize always produces a draft).
    pack.approval.status = "validated"
    pack.status = "validated"

    _write_silver(
        settings,
        "dbc",
        "orders",
        [
            {"order_id": "o1", "amt_value": 100, "stat_value": "accepted"},
            {"order_id": "o2", "amt_value": 50, "stat_value": "accepted"},
            {"order_id": "o3", "amt_value": 25, "stat_value": "declined"},
            {"order_id": "o4", "amt_value": 10, "stat_value": "declined"},
        ],
    )

    manifest = compile_pack(settings, pack)
    assert manifest["status"] == "compiled"

    rows = read_staging_output(settings, "order_kpis")
    by_id = {row["kpi_id"]: row["value"] for row in rows}
    assert by_id["order_total"] == 185
    assert by_id["accept_rate"] == 0.5


def test_promote_mapping_writes_governance_pack_and_updates_materialized_kpis(settings, template) -> None:
    mapping = _fully_approved_mapping()
    result = promote_mapping(settings, template, mapping, version="1.0.0")
    assert Path(result["pack_path"]).is_file()
    assert set(result["report"]["included_kpis"]) == {"order_total", "accept_rate", "accepted_count", "total_count"}
    assert set(mapping.materialized_kpi_ids) == {"order_total", "accept_rate", "accepted_count", "total_count"}

    reloaded = load_mapping(settings)
    assert reloaded is not None
    assert set(reloaded.materialized_kpi_ids) == {"order_total", "accept_rate", "accepted_count", "total_count"}
