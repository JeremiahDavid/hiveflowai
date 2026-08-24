"""Portal tests for the Data Profile Explorer UI."""

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


def _write_customers(tmp_path: Path) -> None:
    out = prefix_path(tmp_path, silver_stg_entity_prefix("dbc", "customers"))
    write_parquet_local(
        out,
        "data.parquet",
        [
            {"id": "c1", "displayName": "Acme", "email": "billing@acme.com"},
            {"id": "c2", "displayName": "Beta", "email": "ap@beta.com"},
        ],
    )


def _login(client: Client) -> None:
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})


def test_index_lists_unprofiled_table(tmp_path: Path, portal_env: None) -> None:
    _write_customers(tmp_path)
    client = _client(tmp_path)
    _login(client)

    response = client.get("/portal/dna/data-profile")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "customers" in html
    assert "Not profiled" in html
    assert "Profile now" in html


def test_refresh_entity_profiles_and_redirects(tmp_path: Path, portal_env: None) -> None:
    _write_customers(tmp_path)
    client = _client(tmp_path)
    _login(client)

    response = client.post(
        "/portal/dna/data-profile/dbc/customers", data={"action": "refresh_entity"}
    )
    assert response.status_code == 302
    assert "msg=" in response.headers["Location"]

    detail = client.get("/portal/dna/data-profile/dbc/customers")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert "customers" in html
    assert "Re-run Bedrock" in html
    assert "Save overrides" in html

    index = client.get("/portal/dna/data-profile")
    assert "Profiled" in index.get_data(as_text=True)


def test_save_overrides_marks_edited(tmp_path: Path, portal_env: None) -> None:
    _write_customers(tmp_path)
    client = _client(tmp_path)
    _login(client)

    client.post("/portal/dna/data-profile/dbc/customers", data={"action": "refresh_entity"})
    response = client.post(
        "/portal/dna/data-profile/dbc/customers",
        data={
            "action": "save_overrides",
            "purpose": "Corrected purpose text",
            "description__id": "Corrected id description",
        },
    )
    assert response.status_code == 302

    from hiveflow.dna.data_profile import load_entity_profile

    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    profile = load_entity_profile(settings, "dbc", "customers")
    assert profile is not None
    assert profile["purpose"] == "Corrected purpose text"
    assert profile["purpose_edited"] is True
    by_name = {f["name"]: f for f in profile["fields"]}
    assert by_name["id"]["description"] == "Corrected id description"
    assert by_name["id"]["description_edited"] is True

    detail = client.get("/portal/dna/data-profile/dbc/customers")
    assert "edited" in detail.get_data(as_text=True).lower()


def test_source_refresh_profiles_all_entities(tmp_path: Path, portal_env: None) -> None:
    _write_customers(tmp_path)
    client = _client(tmp_path)
    _login(client)

    response = client.post("/portal/dna/data-profile/dbc/refresh", data={})
    assert response.status_code == 302
    assert "msg=" in response.headers["Location"]

    index = client.get("/portal/dna/data-profile")
    assert "Profiled" in index.get_data(as_text=True)


def test_detail_page_offers_profile_now_when_missing(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    _login(client)

    response = client.get("/portal/dna/data-profile/dbc/ghost")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Not profiled yet" in html
    assert "Profile now" in html
