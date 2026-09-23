"""DNA Engine web app: shared-portal-session auth path.

DNA Engine is its own FastAPI app/Lambda but has no login of its own — it
trusts the same signed ``hiveflow_portal_session`` cookie the real portal
issues (see ``hiveflow.dna_engine.web.auth``), same trust boundary as
Spreadsheet Engine (``test_spreadsheet_engine_web.py``). Tenant resolution
(``hiveflow.dna_engine.web.tenant``) is stubbed out since it needs a real
multi-tenant config.yaml/AWS setup covered elsewhere.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.auth import PortalSession, create_session_token, session_cookie_name
from hiveflow.dna.web.portal.config import ClientPortalConfig


def _fake_client_config(client_id: str = "poc") -> ClientPortalConfig:
    return ClientPortalConfig(
        client_id=client_id,
        display_name="POC Distribution Co.",
        welcome_title="",
        welcome_message="",
        reporting_company="poc",
    )


@pytest.fixture(autouse=True)
def _stub_tenant_scope(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from hiveflow.dna_engine.web import tenant as tenant_module

    fake_settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1", company="poc")

    @contextmanager
    def _fake_scope(client_id: str):
        yield tenant_module.TenantScope(settings=fake_settings, client=_fake_client_config(client_id))

    monkeypatch.setattr(tenant_module, "tenant_scope", _fake_scope)
    monkeypatch.setattr("hiveflow.dna_engine.web.app.tenant_scope", _fake_scope)
    # Bypass the tenant-staleness check (list_configured_portal_client_ids
    # normally reads config.yaml's client registry) so a bare "poc" session
    # is accepted without a real multi-tenant config fixture.
    monkeypatch.setattr("hiveflow.dna_engine.web.app.list_configured_portal_client_ids", lambda _cfg: {"poc"})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIVEFLOW_PORTAL_SESSION_SECRET", "test-shared-secret")

    from hiveflow.dna_engine.web.app import create_dna_engine_app

    return TestClient(create_dna_engine_app())


def _session_cookie(*, username: str = "jane", client_id: str = "poc") -> str:
    import time

    session = PortalSession(username=username, client_id=client_id, issued_at=int(time.time()))
    return create_session_token(session, company="poc", environment="dev")


def test_no_session_redirects_to_portal_login(client: TestClient) -> None:
    response = client.get("/dna", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/portal/login?next=")


def test_no_session_post_is_401(client: TestClient) -> None:
    response = client.post("/dna", follow_redirects=False)
    assert response.status_code == 401


def test_valid_shared_session_reaches_the_app(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna.web.portal.catalog.list_catalog_tables", lambda settings: [])
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)
    response = client.get("/catalog", follow_redirects=False)
    assert response.status_code == 200
    assert b"No gold tables yet" in response.content


def test_tampered_session_cookie_is_rejected(client: TestClient) -> None:
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token[:-1] + ("0" if token[-1] != "0" else "1"))
    response = client.get("/catalog", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/portal/login?next=")


def test_stale_tenant_session_is_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.list_configured_portal_client_ids", lambda _cfg: {"other"})
    token = _session_cookie(client_id="poc")
    client.cookies.set(session_cookie_name(), token)
    response = client.get("/catalog", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/portal/login?next=")


def test_dna_landing_redirects_to_catalog_first_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hiveflow.dna.schema import OutputSpec

    monkeypatch.setattr(
        "hiveflow.dna.web.portal.catalog.list_catalog_tables",
        lambda settings: [OutputSpec(id="out_demo", output_type="table", build="", columns=["id"])],
    )
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)
    response = client.get("/dna", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/catalog/out_demo"


def _write_customers(tmp_path) -> None:
    from hiveflow.storage.parquet import write_parquet_local
    from hiveflow.storage.paths import prefix_path, silver_stg_entity_prefix

    out = prefix_path(tmp_path, silver_stg_entity_prefix("dbc", "customers"))
    write_parquet_local(
        out,
        "data.parquet",
        [
            {"id": "c1", "displayName": "Acme", "email": "billing@acme.com"},
            {"id": "c2", "displayName": "Beta", "email": "ap@beta.com"},
        ],
    )


def test_data_profile_index_lists_unprofiled_table(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    _write_customers(tmp_path)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/dna/data-profile", follow_redirects=False)
    assert response.status_code == 200
    assert "customers" in response.text
    assert "Not profiled" in response.text
    assert "Profile now" in response.text


def test_data_profile_refresh_entity_and_save_overrides(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.data_profile import load_entity_profile
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    _write_customers(tmp_path)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    refreshed = client.post(
        "/dna/data-profile/dbc/customers", data={"action": "refresh_entity"}, follow_redirects=False
    )
    assert refreshed.status_code == 302
    assert "msg=" in refreshed.headers["location"]

    saved = client.post(
        "/dna/data-profile/dbc/customers",
        data={
            "action": "save_overrides",
            "purpose": "Corrected purpose text",
            "description__id": "Corrected id description",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 302

    profile = load_entity_profile(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), "dbc", "customers")
    assert profile is not None
    assert profile["purpose"] == "Corrected purpose text"
    assert profile["purpose_edited"] is True


def test_data_profile_write_actions_require_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.is_portal_admin", lambda username: False)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.post(
        "/dna/data-profile/dbc/customers", data={"action": "refresh_entity"}, follow_redirects=False
    )
    assert response.status_code == 403


def _write_patients(tmp_path) -> None:
    from hiveflow.storage.parquet import write_parquet_local
    from hiveflow.storage.paths import prefix_path, silver_stg_entity_prefix

    out = prefix_path(tmp_path, silver_stg_entity_prefix("dbc", "patient"))
    write_parquet_local(
        out,
        "data.parquet",
        [
            {
                "patient_key": "p1",
                "first_name": "A",
                "last_name": "B",
                "date_of_birth": "2000-01-01",
                "status": "active",
            },
            {
                "patient_key": "p2",
                "first_name": "C",
                "last_name": "D",
                "date_of_birth": "1999-01-01",
                "status": "active",
            },
        ],
    )


def test_model_mapping_index_shows_industry_picker(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/dna/model-mapping", follow_redirects=False)
    assert response.status_code == 200
    assert "Dental" in response.text
    assert "Initialize mapping" in response.text


def test_model_mapping_init_and_approve_all(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    _write_patients(tmp_path)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    init = client.post(
        "/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"}, follow_redirects=False
    )
    assert init.status_code == 302

    index = client.get("/dna/model-mapping", follow_redirects=False)
    assert "patient" in index.text
    assert "% complete" in index.text

    approved = client.post(
        "/dna/model-mapping",
        data={"action": "approve_all", "entity_id": "patient"},
        follow_redirects=False,
    )
    assert approved.status_code == 302
    assert "entity=patient" in approved.headers["location"]


def test_model_mapping_write_actions_require_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.is_portal_admin", lambda username: False)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.post(
        "/dna/model-mapping", data={"action": "init", "industry_pack_id": "dental"}, follow_redirects=False
    )
    assert response.status_code == 403


def _seed_source_docs_gold(settings: DnaSettings) -> None:
    from hiveflow.dna.store import write_yaml_artifact
    from hiveflow.storage.paths import governance_source_docs_gold_key

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


def test_source_docs_inspector_empty_shows_build(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/semantics/source-docs", follow_redirects=False)
    assert response.status_code == 200
    assert "Source Browser" in response.text
    assert "Business Central" in response.text

    response_dbc = client.get("/semantics/source-docs/dbc", follow_redirects=False)
    assert response_dbc.status_code == 200
    assert "Business Central" in response_dbc.text


def test_source_docs_gold_api(client: TestClient, tmp_path) -> None:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="poc")
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(settings, company="poc")
    _seed_source_docs_gold(settings)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/api/source-docs-gold?source=dbc", follow_redirects=False)
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"]["table_count"] == 1
    assert payload["source"] == "dbc"


def test_source_docs_gold_write_actions_require_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.is_portal_admin", lambda username: False)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.post("/api/source-docs-gold/build", json={"source": "dbc"}, follow_redirects=False)
    assert response.status_code == 403


def test_governance_after_login(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/governance", follow_redirects=False)
    assert response.status_code == 200
    assert "Pack Registry" in response.text


def test_governance_restricted_for_non_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.is_portal_admin", lambda username: False)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.post("/governance", data={"action": "restore_dna"}, follow_redirects=False)
    assert response.status_code == 403


def test_governance_users_requires_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.is_portal_admin", lambda username: False)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/governance/users", follow_redirects=False)
    assert response.status_code == 403


def test_governance_users_lists_legacy_users(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "jane")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")
    token = _session_cookie(username="jane")
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/governance/users", follow_redirects=False)
    assert response.status_code == 200


def test_kpi_generator_after_login(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/dna/kpi-generator", follow_redirects=False)
    assert response.status_code == 200
    assert "Manual refreshes remaining" in response.text


def test_kpi_generator_manual_dna_refresh_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from hiveflow.dna.init_client import init_client_governance

    monkeypatch.setenv("HIVEFLOW_DNA_REFRESH_MOCK", "1")
    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.post(
        "/dna/kpi-generator", data={"action": "manual_dna_refresh"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "DNA refresh started" in response.text


def test_kpi_generator_status_json(client: TestClient, tmp_path) -> None:
    from hiveflow.dna.init_client import init_client_governance

    init_client_governance(DnaSettings(source="dbc", data_dir=tmp_path, company="poc"), company="poc")
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.get("/dna/kpi-generator/status?proposal_id=missing", follow_redirects=False)
    assert response.status_code == 200
    payload = response.json()
    assert payload["proposal_id"] == "missing"
    assert payload["generation_status"] == "complete"


def test_kpi_generator_write_actions_require_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hiveflow.dna_engine.web.app.is_portal_admin", lambda username: False)
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)

    response = client.post("/dna/kpi-generator", data={"action": "manual_dna_refresh"}, follow_redirects=False)
    assert response.status_code == 403
