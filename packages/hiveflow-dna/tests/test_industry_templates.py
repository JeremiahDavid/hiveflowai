"""Tests for industry data model templates (schema, loader, content)."""

from __future__ import annotations

import copy

import pytest
import yaml

from hiveflow.dna.industry_templates import (
    industry_pack_dir,
    list_available_industry_templates,
    load_industry_template,
    load_industry_template_by_id,
    load_industry_template_file,
)

_VALID_PAYLOAD = {
    "pack_id": "widgets",
    "version": "1.0.0",
    "status": "draft",
    "entities": [
        {
            "id": "order",
            "grain": "one row per order",
            "primary_key": "order_key",
            "fields": [
                {"id": "order_key", "required": True},
                {"id": "amount", "type": "currency"},
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
        }
    ],
}


def test_valid_payload_loads() -> None:
    template = load_industry_template(_VALID_PAYLOAD)
    assert template.pack_id == "widgets"
    assert template.entity_by_id("order").grain == "one row per order"
    assert template.kpi_by_id("order_total").value_field == "amount"


def test_missing_required_top_level_field_rejected() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    del payload["status"]
    with pytest.raises(ValueError, match="schema validation failed"):
        load_industry_template(payload)


def test_kpi_referencing_unknown_entity_rejected() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["kpis"][0]["entity_id"] = "does_not_exist"
    with pytest.raises(ValueError, match="unknown entity_id"):
        load_industry_template(payload)


def test_kpi_referencing_unknown_value_field_rejected() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["kpis"][0]["value_field"] = "does_not_exist"
    with pytest.raises(ValueError, match="unknown value_field"):
        load_industry_template(payload)


def test_ratio_kpi_requires_numerator_and_denominator() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["kpis"][0]["formula_type"] = "ratio"
    del payload["kpis"][0]["value_field"]
    with pytest.raises(ValueError, match="requires numerator_kpi and denominator_kpi"):
        load_industry_template(payload)


def test_ratio_kpi_required_fields_resolve_transitively_through_siblings() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["kpis"] = [
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
    ]
    template = load_industry_template(payload)
    required = template.required_fields_for_kpi("accept_rate")
    assert required == ["order.status"]


def test_cyclic_kpi_dependency_rejected() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["kpis"] = [
        {
            "id": "a",
            "name": "A",
            "entity_id": "order",
            "formula_type": "ratio",
            "numerator_kpi": "b",
            "denominator_kpi": "b",
        },
        {
            "id": "b",
            "name": "B",
            "entity_id": "order",
            "formula_type": "ratio",
            "numerator_kpi": "a",
            "denominator_kpi": "a",
        },
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        load_industry_template(payload)


def test_unknown_primary_key_rejected() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["entities"][0]["primary_key"] = "does_not_exist"
    with pytest.raises(ValueError, match="primary_key"):
        load_industry_template(payload)


def test_calendar_date_field_must_resolve() -> None:
    payload = copy.deepcopy(_VALID_PAYLOAD)
    payload["calendar"] = {
        "id": "cal",
        "type": "calendar_year",
        "date_field": "order.does_not_exist",
        "period_grain": "month",
    }
    with pytest.raises(ValueError, match="unknown field"):
        load_industry_template(payload)


@pytest.mark.parametrize("pack_id", ["dental", "manufacturing"])
def test_shipped_industry_templates_load_and_validate(pack_id: str) -> None:
    template = load_industry_template_by_id(pack_id)
    assert template.pack_id == pack_id
    assert template.entities
    assert template.kpis
    # Every KPI's required fields must resolve without raising.
    for kpi in template.kpis:
        fields = template.required_fields_for_kpi(kpi.id)
        assert isinstance(fields, list)


def test_shipped_templates_have_auto_materializable_and_catalog_only_kpis() -> None:
    dental = load_industry_template_by_id("dental")
    statuses = {kpi.id: kpi.auto_materializable for kpi in dental.kpis}
    assert statuses["production_per_visit"] is True
    assert statuses["ar_aging_by_payer"] is False

    manufacturing = load_industry_template_by_id("manufacturing")
    statuses = {kpi.id: kpi.auto_materializable for kpi in manufacturing.kpis}
    assert statuses["fill_rate"] is True
    assert statuses["customer_margin"] is False


def test_list_available_industry_templates_includes_shipped_packs() -> None:
    available = list_available_industry_templates()
    assert "dental" in available
    assert "manufacturing" in available


def test_load_industry_template_by_id_unknown_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_industry_template_by_id("not_a_real_industry")


def test_industry_pack_dir_contains_shipped_yaml_files() -> None:
    directory = industry_pack_dir()
    assert (directory / "dental_v1.yaml").is_file()
    assert (directory / "manufacturing_v1.yaml").is_file()


def test_load_industry_template_file_round_trip(tmp_path) -> None:
    path = tmp_path / "widgets_v1.yaml"
    path.write_text(yaml.safe_dump(_VALID_PAYLOAD), encoding="utf-8")
    template = load_industry_template_file(path)
    assert template.pack_id == "widgets"
