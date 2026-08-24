"""Portal tests for the Model Mapping UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.test import Client

from hiveflow.dna.init_client import init_client_governance
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import create_app
from hiveflow.project_config import load_project_config
from hiveflow.storage.parquet import write_parquet_local
from hiveflow.storage.paths import prefix_path, silver_stg_entity_prefix


@pytest.fixture
def portal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")


def _client(tmp_path: Path) -> Client:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    init_client_governance(settings, company="POC")
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


def _login(client: Client) -> None:
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})


def _write_patients(tmp_path: Path) -> None:
    out = prefix_path(tmp_path, silver_stg_entity_prefix("dbc", "patient"))
    write_parquet_local(
        out,
        "data.parquet",
        [
            {"patient_key": "p1", "first_name": "A", "last_name": "B", "date_of_birth": "2000-01-01", "status": "active"},
            {"patient_key": "p2", "first_name": "C", "last_name": "D", "date_of_birth": "1999-01-01", "status": "active"},
        ],
    )


def test_index_shows_industry_picker_before_init(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    _login(client)

    response = client.get("/portal/dna/model-mapping")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Dental" in html
    assert "Manufacturing" in html
    assert "Initialize mapping" in html


def test_init_creates_mapping_and_lists_entities(tmp_path: Path, portal_env: None) -> None:
    _write_patients(tmp_path)
    client = _client(tmp_path)
    _login(client)

    response = client.post(
        "/portal/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"}
    )
    assert response.status_code == 302

    index = client.get("/portal/dna/model-mapping")
    html = index.get_data(as_text=True)
    assert "patient" in html
    assert "provider" in html
    assert "% complete" in html


def test_approve_all_then_view_field_list(tmp_path: Path, portal_env: None) -> None:
    _write_patients(tmp_path)
    client = _client(tmp_path)
    _login(client)

    client.post("/portal/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"})
    response = client.post(
        "/portal/dna/model-mapping", data={"action": "approve_all", "entity_id": "patient"}
    )
    assert response.status_code == 302
    assert "entity=patient" in response.headers["Location"]

    detail = client.get("/portal/dna/model-mapping?entity=patient")
    html = detail.get_data(as_text=True)
    assert "Fields — patient" in html
    assert "approved" in html


def test_reject_field_and_add_custom_field(tmp_path: Path, portal_env: None) -> None:
    _write_patients(tmp_path)
    client = _client(tmp_path)
    _login(client)

    client.post("/portal/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"})
    client.post(
        "/portal/dna/model-mapping",
        data={"action": "reject_field", "entity_id": "patient", "field_id": "status"},
    )
    client.post(
        "/portal/dna/model-mapping",
        data={
            "action": "add_field",
            "entity_id": "patient",
            "field_id": "custom_note",
            "silver_column": "notes_col",
        },
    )

    from hiveflow.dna.industry_mapping import load_mapping

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    mapping = load_mapping(settings)
    assert mapping is not None
    entity = mapping.entity_by_id("patient")
    assert entity.field_by_id("status").status == "rejected"
    assert entity.field_by_id("custom_note").silver_column == "notes_col"
    assert entity.field_by_id("custom_note").status == "approved"


def test_exclude_entity_removes_it_from_list(tmp_path: Path, portal_env: None) -> None:
    _write_patients(tmp_path)
    client = _client(tmp_path)
    _login(client)

    client.post("/portal/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"})
    response = client.post(
        "/portal/dna/model-mapping", data={"action": "exclude_entity", "entity_id": "claim"}
    )
    assert response.status_code == 302
    assert "err=" not in response.headers["Location"]

    from hiveflow.dna.industry_mapping import load_mapping

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    mapping = load_mapping(settings)
    assert mapping is not None
    assert "claim" in mapping.excluded_entities


def test_promote_reports_included_and_skipped_kpis(tmp_path: Path, portal_env: None) -> None:
    _write_patients(tmp_path)
    client = _client(tmp_path)
    _login(client)

    client.post("/portal/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"})
    client.post("/portal/dna/model-mapping", data={"action": "approve_all", "entity_id": "patient"})
    response = client.post(
        "/portal/dna/model-mapping", data={"action": "promote", "version": "1.0.0"}
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert "msg=" in location
    assert "skipped" in location.lower() or "Promoted" in location

    from hiveflow.dna.industry_mapping import load_mapping

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    mapping = load_mapping(settings)
    assert mapping is not None
    # Promotion should have run even though no KPI could fully materialize
    # from just the patient entity — the call must not raise.
    assert isinstance(mapping.materialized_kpi_ids, list)
