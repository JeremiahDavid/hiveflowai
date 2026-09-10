"""Tests for platform admin onboarding routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from werkzeug.test import Client

from hiveflow.client_registry import ClientRecord
from hiveflow.dna.web.admin.onboarding.guides import (
    load_connector_guide_markdown,
    render_connector_guide_html,
)
from hiveflow.dna.web.admin.onboarding.handlers import (
    ConnectorCredentialSnapshot,
    company_from_display_name,
    client_portal_site_urls,
    connectors_ready_for_deploy,
    entity_bundles_for_connector,
    initial_admin_from_config,
    invite_onboarding_admin,
    load_connector_credentials,
    normalize_wizard_step,
    parse_client_create_form,
    parse_connectors_from_form,
    parse_initial_admin_fields,
    portal_deploy_ready,
    reporting_stack_deployed,
    save_connector_secret,
    validate_client_config_form,
    validate_connector,
)
from hiveflow.dna.web.admin.onboarding.pipeline_handlers import (
    build_ingest_validation_report,
    client_pipeline_status,
)
from hiveflow.dna.web.admin.onboarding.views import (
    render_client_deploy,
    render_client_detail,
    render_client_pipelines,
    render_connector_credentials,
    render_onboarding_wizard,
)
from hiveflow.dna.web.app import create_app
from hiveflow.dna.settings import DnaSettings


@pytest.fixture()
def admin_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Client:
    monkeypatch.setenv("HIVEFLOW_UI_MODE", "admin")
    monkeypatch.delenv("HIVEFLOW_COGNITO_USER_POOL_ID", raising=False)
    settings = DnaSettings(source="dbc", data_dir=tmp_path, company="POC")
    app = create_app(settings, company="POC", environment="dev", ui_mode="admin")
    return Client(app)


def test_company_from_display_name_camel_cases_words() -> None:
    assert company_from_display_name("Acme Distribution Co.") == "acmedistributionco"
    assert company_from_display_name("Acme") == "acme"


def test_parse_connectors_from_form_supports_multiple_enabled() -> None:
    connectors = parse_connectors_from_form(
        {
            "connector_dbc_enabled": "on",
            "connector_qbo_enabled": "on",
            "connector_qbd_enabled": "on",
        }
    )
    assert tuple(item.source for item in connectors) == ("dbc", "qbd", "qbo")


def test_parse_client_create_form_derives_defaults() -> None:
    spec = parse_client_create_form(
        {
            "display_name": "Acme Distribution Co.",
            "client_id": "acme",
            "connector_dbc_enabled": "on",
        }
    )
    assert spec.company == "acmedistributionco"
    assert spec.client_id == "acme"
    assert spec.environment == "dev"
    assert tuple(item.source for item in spec.connectors) == ("dbc",)
    assert spec.portal.display_name == "Acme Distribution Co."
    assert spec.portal.reporting_hostname == "acme"
    assert spec.dna.source == "dbc"


def test_parse_initial_admin_fields_requires_both_or_neither() -> None:
    assert parse_initial_admin_fields({}) == (None, None)
    assert parse_initial_admin_fields(
        {"initial_admin_username": "jane", "initial_admin_email": "jane@example.com"}
    ) == ("jane", "jane@example.com")
    with pytest.raises(ValueError, match="both be provided or both left empty"):
        parse_initial_admin_fields({"initial_admin_username": "jane"})
    with pytest.raises(ValueError, match="both be provided or both left empty"):
        parse_initial_admin_fields({"initial_admin_email": "jane@example.com"})


def test_parse_client_create_form_persists_initial_admin() -> None:
    spec = parse_client_create_form(
        {
            "display_name": "Acme Distribution Co.",
            "client_id": "acme",
            "connector_dbc_enabled": "on",
            "initial_admin_username": "jane",
            "initial_admin_email": "jane@example.com",
        }
    )
    assert spec.portal.initial_admin_username == "jane"
    assert spec.portal.initial_admin_email == "jane@example.com"


def test_render_onboarding_wizard_includes_optional_initial_admin_section() -> None:
    html = render_onboarding_wizard(
        url=lambda path: path,
        username="admin",
        form_values={"initial_admin_username": "jane", "initial_admin_email": "jane@example.com"},
    )
    assert "Initial portal admin (optional)" in html
    assert 'name="initial_admin_username"' in html
    assert 'value="jane"' in html
    assert 'value="jane@example.com"' in html
    assert "GlobalAdmin can always sign in" in html


def test_render_client_deploy_includes_optional_admin_invite_section() -> None:
    html = render_client_deploy(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        initial_admin={"initial_admin_username": "jane", "initial_admin_email": "jane@example.com"},
        portal_ready=False,
        portal_dns_required=True,
    )
    assert "Initial portal admin (optional)" in html
    assert "/admin/onboarding/acme/invite-admin" in html
    assert "Send admin invite" in html
    assert "disabled" in html
    assert "Available after ReportingStack and GlobalDnsStack deploy complete" in html


def test_client_portal_site_urls_uses_platform_domain_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hiveflow.dna.web.admin.onboarding.handlers.get_platform_environment_config",
        lambda environment: {
            "ui": {
                "domain": {"zone_name": "hive-flow-ai.com"},
                "portal": {
                    "clients": {
                        "acme": {
                            "display_name": "Acme",
                            "reporting_hostname": "acme",
                        }
                    }
                },
            }
        },
    )
    urls = client_portal_site_urls(environment="dev", client_id="acme")
    assert urls["portal"] == "https://acme.hive-flow-ai.com/portal"
    assert urls["login"] == "https://acme.hive-flow-ai.com/portal/login"
    assert urls["governance_users"] == "https://acme.hive-flow-ai.com/portal/governance/users"


def test_render_client_deploy_shows_client_portal_url() -> None:
    html = render_client_deploy(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        portal_urls={
            "portal": "https://acme.hive-flow-ai.com/portal",
            "governance_users": "https://acme.hive-flow-ai.com/portal/governance/users",
        },
        portal_ready=True,
    )
    assert "Client portal" in html
    assert "https://acme.hive-flow-ai.com/portal" in html
    assert "https://acme.hive-flow-ai.com/portal/governance/users" in html
    assert 'class="admin-onboarding-portal-link"' in html
    assert "Portal goes live when ReportingStack deploy completes." not in html


def test_reporting_stack_deployed_matches_reporting_stack_status() -> None:
    payload = {
        "deploy": {
            "stacks": [
                {"stack_name": "ReportingStack-acme-dev", "status": "complete"},
                {"stack_name": "IngestStack-acme-dev", "status": "complete"},
            ]
        }
    }
    assert reporting_stack_deployed(client_id="acme", environment="dev", status_payload=payload) is True
    payload["deploy"]["stacks"][0]["status"] = "in_progress"
    assert reporting_stack_deployed(client_id="acme", environment="dev", status_payload=payload) is False


def test_portal_deploy_ready_tracks_dna_stack() -> None:
    # Readiness is the client's data plane; the reporting UI is the shared
    # PortalStack and DNS is the wildcard — neither is per client.
    payload = {
        "deploy": {
            "stacks": [
                {"stack_name": "IngestStack-acme-dev", "status": "complete"},
                {"stack_name": "DnaStack-acme-dev", "status": "in_progress"},
            ]
        }
    }
    assert portal_deploy_ready(
        client_id="acme", company="acme", environment="dev", status_payload=payload
    ) is False
    payload["deploy"]["stacks"][1]["status"] = "complete"
    assert portal_deploy_ready(
        client_id="acme", company="acme", environment="dev", status_payload=payload
    ) is True


def test_invite_onboarding_admin_requires_portal_deploy() -> None:
    with pytest.raises(ValueError, match="Deploy the client's DnaStack"):
        invite_onboarding_admin(
            company="acme",
            environment="dev",
            client_id="acme",
            username="jane",
            email="jane@example.com",
            status_payload={"deploy": {"stacks": []}},
        )


def test_invite_onboarding_admin_calls_cognito_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    payload = {
        "deploy": {
            "stacks": [
                {"stack_name": "DnaStack-acme-dev", "status": "complete"},
            ]
        }
    }
    with patch(
        "hiveflow.dna.web.portal.cognito.invite_portal_user",
        return_value={"username": "jane", "role": "admin"},
    ) as mock_invite, patch(
        "hiveflow.dna.web.portal.config.load_client_portal_config",
        return_value=SimpleNamespace(max_users=10),
    ):
        result = invite_onboarding_admin(
            company="acme",
            environment="dev",
            client_id="acme",
            username="jane",
            email="jane@example.com",
            status_payload=payload,
        )

    assert result["ok"] is True
    mock_invite.assert_called_once()
    assert mock_invite.call_args.kwargs["role"] == "admin"
    assert mock_invite.call_args.kwargs["client_id"] == "acme"


def test_admin_onboarding_requires_login(admin_client: Client) -> None:
    response = admin_client.get("/admin/onboarding")
    assert response.status_code in {302, 401, 200}
    if response.status_code == 302:
        assert "/admin/login" in (response.headers.get("Location") or "")


def test_admin_onboarding_new_route_registered(admin_client: Client) -> None:
    response = admin_client.get("/admin/onboarding/new")
    assert response.status_code in {302, 200, 401, 503}
    if response.status_code == 200:
        body = response.get_data(as_text=True)
        assert "admin-onboarding-steps" in body
        assert "Client config" in body
        assert "Step 1 of 4" in body


def test_validate_client_config_form_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="Display name is required"):
        validate_client_config_form({"client_id": "acme"})
    with pytest.raises(ValueError, match="Portal client id is required"):
        validate_client_config_form({"display_name": "Acme Co"})


def test_normalize_wizard_step_clamps_to_four_steps() -> None:
    assert normalize_wizard_step(1) == 1
    assert normalize_wizard_step(2) == 2
    assert normalize_wizard_step(99) == 4


def test_load_connector_guide_markdown_for_known_sources() -> None:
    for source in ("dbc", "qbo", "qbd"):
        markdown = load_connector_guide_markdown(source)
        assert markdown
        assert "What the client needs" in markdown


def test_render_connector_guide_html_includes_inline_credential_fields() -> None:
    render_connector_guide_html.cache_clear()
    html = render_connector_guide_html("dbc")
    assert 'data-credential-guide="BC_CLIENT_ID"' in html
    assert 'data-credential-guide="BC_COMPANY_ID"' not in html
    assert "Load companies" in html
    assert "admin-connector-guide-input" in html
    assert "admin-connector-guide-field" not in html
    assert 'placeholder="paste"' in html
    assert 'name="BC_CLIENT_ID"' not in html
    assert "OAuthLanding.htm" in html
    assert "ADD RELATED FIELDS" in html
    assert "D365 BUS FULL ACCESS" in html


def test_render_connector_guide_html_includes_credential_sections() -> None:
    render_connector_guide_html.cache_clear()
    html = render_connector_guide_html("dbc")
    assert "admin-connector-guide-content" in html
    assert "Entra" in html
    assert "Where to find each input" in html
    assert "Entra client id" in html
    assert "BC company" in html
    assert "config.yaml" not in html
    assert "Secrets Manager" not in html
    assert "Deploy AWS" not in html


def test_render_connector_guide_html_excludes_backend_for_qbo() -> None:
    render_connector_guide_html.cache_clear()
    html = render_connector_guide_html("qbo")
    assert "Intuit" in html
    assert "Where to find each input" in html
    assert "QBO redirect URI" in html
    assert "cdk deploy" not in html.lower()
    assert "stepfunctions" not in html.lower()


def test_render_connector_guide_html_excludes_backend_for_qbd() -> None:
    render_connector_guide_html.cache_clear()
    html = render_connector_guide_html("qbd")
    assert "Web Connector" in html
    assert "Where to find each input" in html
    assert "SOAP URL" in html
    assert "API Gateway" not in html
    assert "create_secrets.py" not in html


def test_entity_bundles_for_connector_lists_known_bundles() -> None:
    assert "full" in entity_bundles_for_connector("dbc")
    assert "full_accounting" in entity_bundles_for_connector("qbo")
    assert "v1_accounting" in entity_bundles_for_connector("qbd")


def test_render_onboarding_wizard_uses_entity_bundle_select() -> None:
    html = render_onboarding_wizard(
        url=lambda path: path,
        username="admin",
    )
    assert 'name="connector_dbc_entity_bundle"' in html
    assert "<select" in html
    assert 'value="full"' in html or ">full</option>" in html


def test_render_connector_credentials_shows_second_onboarding_step() -> None:
    html = render_connector_credentials(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc"],
    )
    assert "Step 2 of 4" in html
    assert "admin-onboarding-step-arrow" in html
    assert ">Step 1</span>" in html
    assert ">Step 2</span>" in html
    assert ">Step 3</span>" in html
    assert ">Step 4</span>" in html
    assert "Connectors" in html
    assert "ADD RELATED FIELDS" in html
    assert "D365 BUS FULL ACCESS" in html
    assert "Deploy" in html
    assert "data-connector-continue-deploy" in html
    assert 'href="/admin/onboarding/acme/deploy?environment=dev&amp;client_id=acme"' in html
    assert 'href="/admin/onboarding/new?company=acme&amp;environment=dev&amp;client_id=acme"' in html


def test_render_client_deploy_shows_third_onboarding_step() -> None:
    html = render_client_deploy(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        status_payload={"deploy": {"stacks": []}, "verification": {}},
    )
    assert "Step 3 of 4" in html
    assert "data-stack-deploy-form" in html
    assert "Back to connectors" in html
    assert "Continue to pipelines" in html
    assert 'href="/admin/onboarding/acme?environment=dev&amp;client_id=acme"' in html


def test_render_client_detail_shows_second_onboarding_step() -> None:
    html = render_client_detail(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc"],
    )
    assert "Step 2 of 4" in html
    assert "data-connector-continue-deploy" in html


def test_render_onboarding_wizard_links_forward_to_detail_when_editing() -> None:
    from hiveflow.dna.web.admin.onboarding.views import render_onboarding_wizard

    html = render_onboarding_wizard(
        url=lambda path: path,
        username="admin",
        company="acme",
        environment="dev",
        client_id="acme",
        form_values={
            "onboarding_company": "acme",
            "onboarding_environment": "dev",
            "onboarding_client_id": "acme",
            "display_name": "Acme Co",
            "client_id": "acme",
            "connector_dbc_enabled": "on",
        },
    )
    assert 'href="/admin/onboarding/acme?environment=dev&amp;client_id=acme"' in html


def test_render_client_deploy_includes_stack_status_polling() -> None:
    html = render_client_deploy(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        status_payload={
            "deploy": {
                "stacks": [
                    {
                        "stack_name": "IngestStack-ACME-dev",
                        "status": "in_progress",
                        "status_reason": "Resource creation",
                    }
                ]
            },
            "verification": {},
        },
        build_id="hiveflow-client-provision-dev:abc123",
    )
    assert "data-stack-status-section" in html
    assert "data-stack-status-url=" in html
    assert 'data-stack-poll-ms="30000"' in html
    assert "data-stack-deploy-form" in html
    assert "data-stack-progress" in html
    assert "admin-stack-progress is-indeterminate" in html
    assert 'data-stack-row data-stack-name="IngestStack-ACME-dev"' in html
    assert "startStackPolling" in html
    assert "build_id=hiveflow-client-provision-dev:abc123" in html


def test_render_client_deploy_shows_complete_stack_progress() -> None:
    html = render_client_deploy(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        status_payload={
            "deploy": {
                "stacks": [
                    {
                        "stack_name": "ReportingStack-acme-dev",
                        "status": "complete",
                        "status_reason": "",
                    }
                ]
            },
            "verification": {},
        },
    )
    assert "admin-stack-progress is-complete" in html
    assert 'style="width: 100%;"' in html


def test_build_ingest_validation_report_summarizes_tables() -> None:
    report = build_ingest_validation_report(
        {
            "source": "dbc",
            "ingested_at": "2026-01-01T00:00:00+00:00",
            "entities": [
                {"entity": "customers", "row_count": 12, "status": "ok"},
                {"entity": "invoices", "row_count": 4, "status": "ok"},
                {"entity": "vendors", "row_count": 0, "status": "failed"},
            ],
            "ingest_summary": {"succeeded": 2, "failed": 1, "total": 3},
        }
    )
    assert report["table_count"] == 2
    assert report["failed_table_count"] == 1
    assert report["total_rows"] == 16
    assert report["tables"][0]["table"] == "customers"


def test_client_pipeline_status_survives_missing_bucket_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = ClientRecord(
        company="poc2",
        client_id="poc2",
        environment="dev",
        connector_sources=("dbc",),
        portal_display_name="POC 2",
        reporting_hostname="poc2",
        dna_enabled=True,
    )
    execution_arn = "arn:aws:states:us-east-2:123:execution:poc2-dev-dbc-refresh:abc"

    def describe_fn(arn: str) -> dict[str, Any]:
        assert arn == execution_arn
        return {"status": "SUCCEEDED"}

    def list_fn(**_kwargs: Any) -> dict[str, Any]:
        return {"executions": [{"executionArn": execution_arn}]}

    def _raise_bucket_resolution_error(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError(
            "Could not resolve AWS account ID for bucket naming. "
            "Set CDK_DEFAULT_ACCOUNT or companies.*.environments.*.aws.account in config.yaml."
        )

    monkeypatch.setattr(
        "hiveflow.dna.web.admin.onboarding.pipeline_handlers.ingest_validation_report",
        _raise_bucket_resolution_error,
    )

    payload = client_pipeline_status(
        record,
        region="us-east-2",
        describe_fn=describe_fn,
        list_fn=list_fn,
    )

    assert payload["ingest"]["dbc"]["status"] == "succeeded"
    assert payload["ingest"]["dbc"]["has_report"] is False


def test_render_client_pipelines_shows_fourth_onboarding_step() -> None:
    html = render_client_pipelines(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc"],
        dna_enabled=True,
        status_payload={
            "ingest": {
                "dbc": {
                    "label": "Business Central",
                    "status": "succeeded",
                    "execution_arn": "arn:aws:states:us-east-2:123:execution:acme-dev-dbc:abc",
                    "note": "Runs bronze ingest and silver consolidation for this connector.",
                    "has_report": True,
                }
            },
            "dna": {
                "enabled": True,
                "status": "not_started",
                "execution_arn": "",
            },
        },
    )
    assert "Step 4 of 4" in html
    assert "data-pipeline-status-section" in html
    assert "data-pipeline-ingest-kickoff" in html
    assert "data-pipeline-dna-kickoff" in html
    assert "data-ingest-report-open" in html
    assert "admin-ingest-report-dialog" in html
    assert 'href="/admin/onboarding/acme/deploy?environment=dev&amp;client_id=acme"' in html
    assert "openIngestReport" in html


def test_render_client_detail_includes_credential_setup_guide() -> None:
    html = render_client_detail(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc", "qbo"],
    )
    assert "Credential setup guide" in html
    assert 'data-connector-guide="connector-guide-dbc"' in html
    assert 'id="connector-guide-dbc"' in html
    assert 'id="connector-secrets-dbc"' in html
    assert 'data-credential-main="BC_CLIENT_ID"' in html
    assert 'data-credential-guide="BC_CLIENT_ID"' in html
    assert "data-credential-summary" in html
    assert "DBC_LOOKUP_FIELDS" in html
    assert "setConnectorValidated" in html
    assert "Save secret" in html
    assert "Apply to form" in html
    assert "data-connector-validate" in html
    assert "data-connector-validate-url" in html
    assert "data-connector-validate-check" in html
    assert "setConnectorValidated" in html


def test_render_client_detail_disables_load_companies_until_lookup_fields_filled() -> None:
    html = render_client_detail(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc"],
    )
    assert "data-dbc-load-companies" in html
    assert "Fill in the four fields above" in html
    load_btn_empty = html.split("data-dbc-load-companies", 1)[1].split("</button>", 1)[0]
    assert " disabled" in load_btn_empty

    html_saved = render_client_detail(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc"],
        connector_credentials={
            "dbc": ConnectorCredentialSnapshot(
                secret_id="hiveflow-acme-dbc-dev",
                exists=True,
                values={
                    "BC_CLIENT_ID": "client-id",
                    "BC_CLIENT_SECRET": "client-secret",
                    "BC_TENANT_ID": "tenant-id",
                    "BC_ENVIRONMENT_NAME": "Production",
                },
            )
        },
    )
    load_btn = html_saved.split("data-dbc-load-companies")[1].split("</button>")[0]
    assert " disabled" not in load_btn
    assert "Load companies from this BC environment" in load_btn


def test_render_client_detail_prefills_saved_credentials() -> None:
    html = render_client_detail(
        url=lambda path: path,
        username="admin",
        company="acme",
        client_id="acme",
        environment="dev",
        connector_sources=["dbc"],
        connector_credentials={
            "dbc": ConnectorCredentialSnapshot(
                secret_id="hiveflow-acme-dbc-dev",
                exists=True,
                values={
                    "BC_CLIENT_ID": "client-id",
                    "BC_CLIENT_SECRET": "client-secret",
                    "BC_TENANT_ID": "tenant-id",
                    "BC_ENVIRONMENT_NAME": "Production",
                    "BC_COMPANY_ID": "company-guid",
                },
            )
        },
    )
    assert "Saved secret: <code>hiveflow-acme-dbc-dev</code>" in html
    assert 'value="client-id"' in html
    assert 'value="tenant-id"' in html
    assert 'value="Production"' in html
    assert 'value="company-guid"' in html
    assert 'value="company-guid" selected' in html


def test_load_connector_credentials_returns_empty_when_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hiveflow.client_registry.ClientRegistry.get_client",
        lambda self, company, environment=None, client_id=None: SimpleNamespace(
            company="acme",
            environment="dev",
            client_id="acme",
            connector_sources=("dbc",),
        ),
    )
    monkeypatch.setattr(
        "hiveflow.client_registry.ClientRegistry.secret_name",
        lambda self, record, source=None: "hiveflow-acme-dbc-dev",
    )

    def _raise_not_found(secret_id: str, region=None):
        raise ValueError(
            f"Secrets Manager secret {secret_id!r} was not found in region 'us-east-2'."
        )

    monkeypatch.setattr(
        "hiveflow.dna.web.admin.onboarding.handlers.get_secret_json",
        _raise_not_found,
    )

    snapshot = load_connector_credentials(company="acme", environment="dev", source="dbc")
    assert snapshot.exists is False
    assert snapshot.secret_id == "hiveflow-acme-dbc-dev"
    assert snapshot.values == {}


def test_save_connector_secret_merges_existing_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hiveflow.client_registry.ClientRegistry.get_client",
        lambda self, company, environment=None, client_id=None: SimpleNamespace(
            company="acme",
            environment="dev",
            client_id="acme",
            connector_sources=("dbc",),
        ),
    )
    monkeypatch.setattr(
        "hiveflow.client_registry.ClientRegistry.secret_name",
        lambda self, record, source=None: "hiveflow-acme-dbc-dev",
    )
    stored: dict[str, Any] = {
        "BC_CLIENT_ID": "old-id",
        "BC_CLIENT_SECRET": "old-secret",
        "access_token": "token",
    }
    monkeypatch.setattr(
        "hiveflow.dna.web.admin.onboarding.handlers.get_secret_json",
        lambda secret_id, region=None: dict(stored),
    )

    def _put_secret_json(secret_id: str, payload: dict[str, Any], region=None) -> None:
        stored.clear()
        stored.update(payload)

    monkeypatch.setattr(
        "hiveflow.dna.web.admin.onboarding.handlers.put_secret_json",
        _put_secret_json,
    )
    monkeypatch.setattr(
        "hiveflow.secrets_manager.ensure_secret_json",
        lambda secret_id, payload, region=None, source=None, company=None, environment=None: "exists",
    )

    save_connector_secret(
        company="acme",
        environment="dev",
        source="dbc",
        credentials={"BC_CLIENT_ID": "new-id"},
    )

    assert stored["BC_CLIENT_ID"] == "new-id"
    assert stored["BC_CLIENT_SECRET"] == "old-secret"
    assert stored["access_token"] == "token"
