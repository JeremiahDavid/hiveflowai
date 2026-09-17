"""Tests for Source Browser (gold source-docs) portal inspector."""

from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.test import Client

from hiveflow.dna.init_client import init_client_governance
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.source_docs.reference import load_source_docs_gold
from hiveflow.dna.store import write_yaml_artifact
from hiveflow.dna.web.app import create_app
from hiveflow.project_config import load_project_config
from hiveflow.storage.paths import (
    governance_source_docs_gold_key,
    governance_source_semantic_latest_profile_key,
)


@pytest.fixture
def portal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")


def _settings(tmp_path: Path) -> DnaSettings:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    init_client_governance(settings, company="POC")
    return settings


def _client(tmp_path: Path) -> Client:
    settings = _settings(tmp_path)
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    return Client(
        create_app(
            settings,
            company="POC",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting",
        )
    )


def _seed_gold(settings: DnaSettings) -> None:
    write_yaml_artifact(
        settings,
        governance_source_docs_gold_key("dbc", "entity_properties.yaml"),
        {
            "source": "dbc",
            "kind": "ms_learn_entity_properties",
            "table_count": 1,
            "property_count": 2,
            "generated_at": "2026-08-11T00:00:00Z",
            "tables": [
                {
                    "silver_entity": "sales_orders",
                    "bc_resource_slug": "salesorder",
                    "description": "A sales order.",
                    "properties": [
                        {"name": "id", "type": "GUID", "description": "Unique ID"},
                        {"name": "status", "type": "string", "description": "Order status"},
                    ],
                }
            ],
        },
    )
    write_yaml_artifact(
        settings,
        governance_source_docs_gold_key("dbc", "entity_relationships.yaml"),
        {
            "source": "dbc",
            "kind": "ms_learn_entity_relationships",
            "table_count": 1,
            "relationship_count": 1,
            "tables": {
                "sales_orders": {
                    "PK": "id",
                    "relationships": [
                        {"target": "customers", "PK": "id", "FK": "customerId"},
                    ],
                }
            },
        },
    )
    write_yaml_artifact(
        settings,
        governance_source_docs_gold_key("dbc", "entity_property_tags.yaml"),
        {
            "source": "dbc",
            "kind": "ms_learn_entity_property_tags",
            "table_count": 1,
            "property_count": 2,
            "tagged_property_count": 1,
            "tables": [
                {
                    "silver_entity": "sales_orders",
                    "properties": [
                        {"name": "id", "tags": []},
                        {"name": "status", "tags": ["order status"]},
                    ],
                }
            ],
        },
    )


def test_load_source_docs_gold_empty(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    payload = load_source_docs_gold(settings)
    assert payload["available"] is False
    assert payload["complete"] is False


def test_load_source_docs_gold_populated(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _seed_gold(settings)
    payload = load_source_docs_gold(settings)
    assert payload["available"] is True
    assert payload["complete"] is True
    assert payload["summary"]["table_count"] == 1
    assert payload["summary"]["relationship_count"] == 1
    assert payload["summary"]["tagged_property_count"] == 1
    assert payload["summary"]["artifact_generated_at"]["entity_properties"] == "2026-08-11T00:00:00Z"
    assert "entity_property_tags" in payload["summary"]["artifact_generated_at"]
    assert "entity_relationships" in payload["summary"]["artifact_generated_at"]


def test_source_docs_inspector_empty_shows_build(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/semantics/source-docs")
    assert response.status_code == 200
    assert b"Source Browser" in response.data
    assert b"source-docs-source-nav" in response.data
    # No source given defaults to the primary configured connector (dbc) —
    # the Spreadsheet Engine is no longer a virtual source living inside this
    # inspector, it's its own portal-authenticated app (see dna_nav.py).
    assert b"Business Central" in response.data

    response_dbc = client.get("/portal/semantics/source-docs/dbc")
    assert response_dbc.status_code == 200
    assert b"Business Central" in response_dbc.data


def test_source_docs_inspector_populated(tmp_path: Path, portal_env: None) -> None:
    settings = _settings(tmp_path)
    _seed_gold(settings)
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(
        create_app(
            settings,
            company="POC",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting",
        )
    )
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/semantics/source-docs/dbc")
    assert response.status_code == 200
    assert b"Rebuild Semantic Model" in response.data
    assert b"Submit changes" in response.data
    assert b"source-docs-submit-btn" in response.data
    assert b"Version history" in response.data
    assert b"Tables" in response.data
    assert b"source-docs-select" in response.data
    assert b">Table<" in response.data
    assert b"source-docs-table-filter" in response.data
    assert b"source-docs-rel-table-filter" in response.data
    assert b"source-docs-tag-table-filter" in response.data
    assert b"source-docs-tag-search" in response.data
    assert b"source-docs-source-nav" in response.data
    assert b"Business Central" in response.data
    assert b"QuickBooks Online" in response.data
    assert b"sales_orders" in response.data
    assert b"order status" in response.data
    assert b"customerId" in response.data
    assert b'data-source-docs-tab="tables"' in response.data
    assert b"sorted by relationship count" in response.data
    assert b"sorted by tag count" in response.data
    assert b"buildIsFresh" in response.data
    assert b"entity_property_tags" in response.data
    assert b'data-kind="table"' in response.data
    assert b'data-kind="relationship"' in response.data
    assert b'data-kind="tag"' in response.data


def test_source_docs_gold_api(tmp_path: Path, portal_env: None) -> None:
    settings = _settings(tmp_path)
    _seed_gold(settings)
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(
        create_app(
            settings,
            company="POC",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting",
        )
    )
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/api/source-docs-gold?source=dbc")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is True
    assert payload["summary"]["table_count"] == 1
    assert payload["source"] == "dbc"
    assert payload["pending_count"] == 0


def test_source_docs_exclude_undo_and_versions_api(tmp_path: Path, portal_env: None) -> None:
    settings = _settings(tmp_path)
    _seed_gold(settings)
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(
        create_app(
            settings,
            company="POC",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting",
        )
    )
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    # Exclude APIs still work for direct/backend use; page does not SSR pending markers.
    exclude = client.post(
        "/api/source-docs-gold/exclude",
        json={"source": "dbc", "kind": "table", "table": "sales_orders"},
    )
    assert exclude.status_code == 200
    assert exclude.get_json()["pending_count"] == 1

    page = client.get("/portal/semantics/source-docs/dbc")
    assert b"source-docs-tag-remove" in page.data
    assert b"hiveflow:source-docs-pending:" in page.data
    assert b"sessionStorage" in page.data

    undo = client.post(
        "/api/source-docs-gold/undo-exclude",
        json={"source": "dbc", "kind": "table", "table": "sales_orders"},
    )
    assert undo.status_code == 200
    assert undo.get_json()["pending_count"] == 0

    # Submit applies client-queued excludes then refuses gold when connectors/global unavailable
    # is not required here — commit path still works after direct exclude.
    client.post(
        "/api/source-docs-gold/exclude",
        json={
            "source": "dbc",
            "kind": "tag",
            "silver_entity": "sales_orders",
            "name": "status",
            "tag": "order status",
        },
    )
    commit = client.post(
        "/api/source-docs-gold/versions/commit",
        json={"source": "dbc", "note": "test commit"},
    )
    assert commit.status_code == 200
    assert commit.get_json()["version"] == 1

    versions = client.get("/api/source-docs-gold/versions?source=dbc")
    assert versions.status_code == 200
    body = versions.get_json()
    assert body["active_version"] == 1
    assert body["pending_count"] == 0

    client.post(
        "/api/source-docs-gold/exclude",
        json={
            "source": "dbc",
            "kind": "relationship",
            "table": "sales_orders",
            "FK": "customerId",
            "target": "customers",
        },
    )
    restore = client.post(
        "/api/source-docs-gold/restore",
        json={"source": "dbc", "version": 1},
    )
    assert restore.status_code == 200
    assert restore.get_json()["version"] == 2
    assert restore.get_json()["entry"]["restored_from"] == 1

    submit = client.post("/api/source-docs-gold/submit", json={"source": "dbc", "excludes": []})
    assert submit.status_code == 400
    assert submit.get_json()["reason"] == "no_pending"


def test_source_docs_submit_applies_queued_excludes(tmp_path: Path, portal_env: None, monkeypatch) -> None:
    settings = _settings(tmp_path)
    _seed_gold(settings)
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]

    def _fake_build(*_args, **_kwargs):
        return {"status": "published", "result": {"ok": True}}

    monkeypatch.setattr(
        "hiveflow.dna.web.portal.semantics.source_docs_service.enqueue_source_docs_gold_build",
        _fake_build,
    )

    client = Client(
        create_app(
            settings,
            company="POC",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting",
        )
    )
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.post(
        "/api/source-docs-gold/submit",
        json={
            "source": "dbc",
            "excludes": [
                {"kind": "table", "table": "sales_orders"},
                {
                    "kind": "tag",
                    "silver_entity": "sales_orders",
                    "name": "status",
                    "tag": "order status",
                },
            ],
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "published"
    assert payload["version"]["version"] == 1
    assert len(payload["applied"]) == 2

    from hiveflow.dna.source_docs.overlays import load_overlay

    props = load_overlay(settings, "entity_properties")
    assert props is not None
    assert "sales_orders" in (props.get("exclude") or {}).get("tables", [])


def test_load_source_docs_gold_includes_silver_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    write_yaml_artifact(
        settings,
        governance_source_semantic_latest_profile_key("dbc"),
        {
            "source": "dbc",
            "kind": "silver_schema_profile",
            "generated_at": "2026-08-13T00:00:00Z",
            "table_count": 1,
            "tables": [
                {
                    "silver_entity": "customers",
                    "glue_table": "silver_dbc_customers",
                    "columns": [{"name": "displayName", "type": "string", "origin": "api"}],
                }
            ],
        },
    )
    payload = load_source_docs_gold(settings, source="dbc")
    assert payload["silver_profile_present"] is True
    assert payload["summary"]["silver_table_count"] == 1
    assert payload["silver_profile"]["tables"][0]["silver_entity"] == "customers"


def test_source_docs_page_hides_silver_catalog_tab(
    tmp_path: Path,
    portal_env: None,
) -> None:
    settings = _settings(tmp_path)
    _seed_gold(settings)
    write_yaml_artifact(
        settings,
        governance_source_semantic_latest_profile_key("dbc"),
        {
            "source": "dbc",
            "kind": "silver_schema_profile",
            "generated_at": "2026-08-13T00:00:00Z",
            "table_count": 1,
            "tables": [
                {
                    "silver_entity": "sales_orders",
                    "glue_table": "silver_dbc_sales_orders",
                    "columns": [{"name": "id", "type": "string", "origin": "api"}],
                }
            ],
        },
    )
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/semantics/source-docs/dbc")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Silver catalog" not in html
    assert "source-docs-panel-silver" not in html
    assert ">MS Learn<" not in html
