"""Tests for the per-client data profiling and description engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveflow.dna.data_profile import (
    load_data_profile_index,
    load_entity_profile,
    profile_entity,
    refresh_data_profile_for_source,
)
from hiveflow.dna.settings import DnaSettings
from hiveflow.storage.parquet import write_parquet_local
from hiveflow.storage.paths import prefix_path, silver_entity_prefix, silver_stg_entity_prefix


@pytest.fixture
def settings(tmp_path: Path) -> DnaSettings:
    return DnaSettings(source="dbc", data_dir=tmp_path, company="POC")


def _write_customers(settings: DnaSettings, *, layer: str = "silver_stg", source: str | None = None) -> None:
    prefix_fn = silver_stg_entity_prefix if layer == "silver_stg" else silver_entity_prefix
    out = prefix_path(settings.data_dir, prefix_fn(source or settings.source, "customers"))
    write_parquet_local(
        out,
        "data.parquet",
        [
            {"id": "c1", "displayName": "Acme", "email": "billing@acme.com"},
            {"id": "c2", "displayName": "Beta", "email": "ap@beta.com"},
            {"id": "c3", "displayName": "Gamma", "email": None},
        ],
    )


def test_profile_entity_heuristic_fallback(settings: DnaSettings) -> None:
    _write_customers(settings)
    profile = profile_entity(settings, "dbc", "customers", invoke=False)

    assert profile["source"] == "dbc"
    assert profile["entity"] == "customers"
    assert profile["row_count"] == 3
    assert profile["purpose"] == "Data from dbc.customers"
    assert "Heuristic fallback" in " ".join(profile["notes"])

    by_name = {f["name"]: f for f in profile["fields"]}
    assert by_name["id"]["description"] == "Column id"
    assert by_name["email"]["inferred_type"] == "email"
    assert by_name["email"]["null_rate"] == pytest.approx(1 / 3)

    persisted = load_entity_profile(settings, "dbc", "customers")
    assert persisted is not None
    assert persisted["row_count"] == 3


def test_profile_entity_uses_stubbed_bedrock_response(settings: DnaSettings) -> None:
    _write_customers(settings)

    def _stub_invoke(_system: str, _user: str) -> str:
        return json.dumps(
            {
                "purpose": "Customer billing contacts",
                "confidence": 0.9,
                "fields": [
                    {"name": "id", "description": "Unique customer identifier"},
                    {"name": "displayName", "description": "Customer display name"},
                    {"name": "email", "description": "Billing contact email"},
                ],
            }
        )

    profile = profile_entity(settings, "dbc", "customers", invoke=_stub_invoke)
    assert profile["purpose"] == "Customer billing contacts"
    assert profile["confidence"] == 0.9
    by_name = {f["name"]: f for f in profile["fields"]}
    assert by_name["id"]["description"] == "Unique customer identifier"
    assert by_name["email"]["description"] == "Billing contact email"


def test_profile_entity_falls_back_on_malformed_bedrock_json(settings: DnaSettings) -> None:
    _write_customers(settings)

    profile = profile_entity(settings, "dbc", "customers", invoke=lambda *_: "not json")
    assert profile["purpose"] == "Data from dbc.customers"
    assert "Heuristic fallback" in " ".join(profile["notes"])


def test_profile_entity_reads_alternate_source(settings: DnaSettings) -> None:
    _write_customers(settings, layer="silver", source="reference")

    profile = profile_entity(settings, "reference", "customers", layer="silver", invoke=False)
    assert profile["source"] == "reference"
    assert profile["row_count"] == 3

    # Written under the client's own governance pack id, not the "reference" source.
    persisted = load_entity_profile(settings, "reference", "customers")
    assert persisted is not None


def test_refresh_data_profile_for_source_profiles_all_entities_and_updates_index(
    settings: DnaSettings,
) -> None:
    _write_customers(settings)
    out = prefix_path(settings.data_dir, silver_stg_entity_prefix(settings.source, "invoices"))
    write_parquet_local(out, "data.parquet", [{"id": "i1", "amount": 100}, {"id": "i2", "amount": 250}])

    result = refresh_data_profile_for_source(settings, "dbc", invoke=False)
    assert result["profiled_count"] == 2
    assert sorted(result["entities"]) == ["customers", "invoices"]
    assert result["errors"] == []

    index = load_data_profile_index(settings)
    tables = {t["entity"]: t for t in index["tables"]}
    assert tables["customers"]["row_count"] == 3
    assert tables["invoices"]["row_count"] == 2
    assert tables["customers"]["source"] == "dbc"


def test_refresh_data_profile_for_source_collects_errors_without_raising(
    settings: DnaSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_customers(settings)
    import hiveflow.dna.data_profile as data_profile_module

    original = data_profile_module.profile_entity

    def _flaky(settings_arg, source, entity, **kwargs):
        if entity == "broken":
            raise RuntimeError("boom")
        return original(settings_arg, source, entity, **kwargs)

    monkeypatch.setattr(data_profile_module, "profile_entity", _flaky)

    result = refresh_data_profile_for_source(
        settings, "dbc", entities=["customers", "broken"], invoke=False
    )
    assert result["profiled_count"] == 1
    assert result["entities"] == ["customers"]
    assert result["errors"] == [{"entity": "broken", "error": "boom"}]


def test_profile_entity_with_no_rows_yet(settings: DnaSettings) -> None:
    profile = profile_entity(settings, "dbc", "ghost_entity", invoke=False)
    assert profile["row_count"] == 0
    assert profile["fields"] == []
