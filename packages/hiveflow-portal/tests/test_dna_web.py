from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from werkzeug.test import Client

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import create_app
from hiveflow.dna.web.portal.routes import _sanitize_portal_next
from hiveflow.dna.web.portal.views import REVENUE_TABLE_LIMIT, aggregate_revenue_by_month
from hiveflow.ingest.storage import write_parquet_local
from hiveflow.project_config import get_environment_config, load_project_config
from hiveflow.storage.paths import prefix_path, silver_entity_prefix


@pytest.fixture
def portal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")


@pytest.fixture
def chart_catalog_env(monkeypatch: pytest.MonkeyPatch, portal_env: None) -> None:
    from hiveflow.dna.web.portal import reporting_layout

    original = reporting_layout.load_reporting_layout

    def _with_chart_catalog(settings, *, override=None):
        layout = dict(override if override is not None else original(settings))
        layout["include_chart_catalog"] = True
        return layout

    monkeypatch.setattr(reporting_layout, "load_reporting_layout", _with_chart_catalog)


@pytest.fixture
def cognito_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_COGNITO_USER_POOL_ID", "us-east-2_TestPool")
    monkeypatch.setenv("HIVEFLOW_COGNITO_CLIENT_ID", "testclient")
    monkeypatch.setenv("HIVEFLOW_COGNITO_REGION", "us-east-2")
    monkeypatch.setenv("HIVEFLOW_PORTAL_DEFAULT_CLIENT_ID", "poc")
    monkeypatch.delenv("HIVEFLOW_PORTAL_USERNAME", raising=False)
    monkeypatch.delenv("HIVEFLOW_PORTAL_PASSWORD", raising=False)


def _client(tmp_path: Path) -> Client:
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    return Client(create_app(settings, company="POC", environment="dev", env_config=env_config))


def test_public_landing_and_pricing(tmp_path: Path) -> None:
    client = _client(tmp_path)

    home = client.get("/")
    assert home.status_code == 200
    assert b"HiveFlowAI" in home.data
    assert b"fraction of the cost" in home.data
    assert b"DMaaS" in home.data
    assert b"DNA Engine" in home.data
    assert b"Business Central" in home.data
    assert b"Reporting Engine" in home.data

    pricing = client.get("/pricing")
    assert pricing.status_code == 200
    assert b"$100" in pricing.data
    assert b"$5,000" in pricing.data


def test_public_platform(tmp_path: Path) -> None:
    """Characterization test for render_platform, added ahead of its Jinja2
    conversion — previously this page had status-code-only coverage."""
    client = _client(tmp_path)

    platform = client.get("/platform")
    assert platform.status_code == 200
    html = platform.data.decode()

    # The five governed layers, each with its numbered badge and heading.
    assert '<span>1</span> Source systems' in html
    assert "Connect ERP, accounting, and ops" in html
    assert '<span>2</span> Data lake' in html
    assert "Three layers — one governed store" in html
    assert '<span>3</span> DNA Engine · AI' in html
    assert "Governed semantics from documentation" in html
    assert '<span>4</span> Reporting Engine · AI' in html
    assert "Presentation without touching calculations" in html
    assert '<span>5</span> Client portal' in html
    assert "Secure delivery to every stakeholder" in html

    # Flow diagram cards and governance section.
    assert 'id="platform-sources"' in html
    assert 'id="platform-governance"' in html
    assert "Scheduled data refresh" in html
    assert "Governed change requests" in html
    assert "platform-emblem-name\">Source systems<" in html
    assert "platform-emblem-name\">DNA Engine<" in html


def test_portal_requires_login(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    response = client.get("/portal")
    assert response.status_code == 302
    assert "/portal/login" in response.headers["Location"]


def test_portal_login_and_overview(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    login = client.post(
        "/portal/login",
        data={"username": "poc", "password": "changeme", "next": "/portal"},
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/portal")

    overview = client.get("/portal")
    assert overview.status_code == 200
    assert b"POC Distribution Co." in overview.data
    assert b"Executive snapshot" in overview.data


def test_portal_governance_after_login(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    governance = client.get("/portal/governance")
    assert governance.status_code == 200
    assert b"Pack Registry" in governance.data
    assert b"poc_dna_config" in governance.data or b"poc_reporting_config" in governance.data
    assert b"Reporting layout pack" in governance.data
    assert b"DNA Engine" in governance.data

    kpi = client.get("/portal/dna/kpi-generator")
    assert kpi.status_code == 200
    assert b"DNA Engine" in kpi.data
    assert b"Manual refreshes remaining" in kpi.data
    assert b"Refresh DNA tables" in kpi.data
    assert b"Refresh gold tables" not in kpi.data
    assert b"Refresh silver tables" not in kpi.data

    legacy = client.get("/portal/semantics", follow_redirects=True)
    assert legacy.status_code == 200
    assert b"Source Browser" in legacy.data or b"source-docs" in legacy.data


def test_portal_manual_dna_refresh_action(
    tmp_path: Path, portal_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_DNA_REFRESH_MOCK", "1")
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    response = client.post(
        "/portal/dna/kpi-generator",
        data={"action": "manual_dna_refresh"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"DNA refresh started" in response.data
    assert b"manual refresh" in response.data.lower()


def test_portal_nav_data_dropdown_and_governance(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    overview = client.get("/portal")
    assert overview.status_code == 200
    assert b"portal-side-nav" in overview.data
    assert b'data-nav-id="reporting"' in overview.data
    assert b">Reporting</a>" in overview.data
    assert b">DNA</a>" in overview.data
    assert b"Executive" in overview.data
    assert b"Revenue trend" in overview.data
    assert b"portal-side-nav-children" in overview.data
    assert b'class="portal-side-nav-link is-child"' in overview.data
    assert b"portal-side-nav-group" in overview.data
    assert b"portal-side-nav-disclosure" in overview.data
    assert b'class="portal-side-nav-link active" href="/portal"' in overview.data
    assert b'class="portal-side-nav-link active" href="/portal/executive"' not in overview.data

    revenue = client.get("/portal/revenue")
    assert revenue.status_code == 200
    assert b"portal-side-nav-group is-open" in revenue.data
    assert b'href="/portal/revenue"' in revenue.data
    assert b"portal-side-nav-link is-child active" in revenue.data
    assert b"portal-side-nav-link has-children is-ancestor" in revenue.data
    assert b">Governance</a>" in overview.data

    catalog = client.get("/portal/catalog", follow_redirects=False)
    assert catalog.status_code == 302
    assert "/portal/catalog/" in catalog.headers["Location"]

    catalog_page = client.get(catalog.headers["Location"])
    assert catalog_page.status_code == 200
    assert b'data-nav-id="dna"' in catalog_page.data
    assert b"Semantic Mappings" not in catalog_page.data
    assert b"Source Browser" in catalog_page.data
    assert b"DNA Catalog" in catalog_page.data
    assert b"DNA Engine" in catalog_page.data
    assert b"Semantic Builder" not in catalog_page.data
    assert b"Semantic Browser" not in catalog_page.data
    assert b"KPI Generator" not in catalog_page.data
    assert b"Gold preview" in catalog_page.data
    assert b"Fact Revenue Lines" in catalog_page.data or b"Dim Customers" in catalog_page.data
    assert b'href="/portal/catalog/out_' in catalog_page.data

    governance = client.get("/portal/governance")
    assert governance.status_code == 200
    assert b'data-nav-id="governance"' in governance.data
    assert b"Pack Registry" in governance.data
    assert b"DNA Engine" in governance.data
    assert b"pack-history-subtitle" in governance.data
    assert b">DNA</div>" in governance.data
    assert b">Reporting</div>" in governance.data
    assert b'class="portal-side-nav-link active" href="/portal/governance"' in governance.data

    kpi = client.get("/portal/dna/kpi-generator")
    assert kpi.status_code == 200
    assert b'data-nav-id="dna"' in kpi.data
    assert b"DNA Engine" in kpi.data
    assert b"Refresh DNA tables" in kpi.data
    assert b"Refresh gold tables" not in kpi.data
    assert b"Refresh silver tables" not in kpi.data

    users = client.get("/portal/governance/users")
    assert users.status_code == 200
    assert b'class="portal-side-nav-link active" href="/portal/governance/users"' in users.data
    assert b'class="portal-side-nav-link active" href="/portal/governance"' not in users.data


def test_portal_catalog_silver_entity_renders_preview_table(tmp_path: Path, portal_env: None) -> None:
    """Characterization test for render_catalog_silver + silver_preview_table_html,
    added ahead of their Jinja2 conversion — previously this route had only a
    status-code-only smoke check with no real entity."""
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    out = prefix_path(settings.data_dir, silver_entity_prefix(settings.source, "customers"))
    write_parquet_local(
        out,
        "data.parquet",
        [{"id": "c1", "displayName": "Acme Corp"}, {"id": "c2", "displayName": "Beta LLC"}],
    )

    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    response = client.get("/portal/catalog/silver/customers")
    assert response.status_code == 200
    assert b"Silver preview" in response.data
    assert b"Showing first" in response.data
    assert b"Acme Corp" in response.data
    assert b"Beta LLC" in response.data
    assert b"Display Name" in response.data or b"displayName" in response.data


def test_portal_catalog_silver_missing_entity_returns_404(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/catalog/silver/not-a-real-entity")
    assert response.status_code == 404


def test_governance_update_section_restricted_for_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")
    monkeypatch.setattr(
        "hiveflow.dna.web.portal.cognito.portal_user_is_admin",
        lambda *args, **kwargs: False,
    )

    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/dna/kpi-generator")
    assert response.status_code == 200
    assert b"DNA Engine is available to portal admins" in response.data


def test_kpi_generator_status_json(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})
    response = client.get("/portal/dna/kpi-generator/status?proposal_id=missing")
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["proposal_id"] == "missing"
    assert payload["generation_status"] == "complete"


def test_api_gateway_stage_prefix(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)

    response = client.get("/", environ_overrides={"SCRIPT_NAME": "/prod"})
    assert response.status_code == 200
    assert b'href="/prod/pricing"' in response.data
    assert b'src="/prod/static/hiveflowai-logo.svg?v=' in response.data

    client.post(
        "/portal/login",
        data={"username": "poc", "password": "changeme"},
        environ_overrides={"SCRIPT_NAME": "/prod"},
    )
    executive = client.get("/portal/executive", environ_overrides={"SCRIPT_NAME": "/prod"})
    assert executive.status_code == 200
    assert b"portal-side-nav" in executive.data
    assert b"Executive" in executive.data
    assert b"Month to date vs prior year" in executive.data
    assert b"kpi-compare-card" in executive.data
    assert b'class="portal-side-nav-link active" href="/prod/portal/executive"' in executive.data
    assert b'class="portal-side-nav-link active" href="/prod/portal"' not in executive.data


def test_execute_api_host_infers_stage_prefix(tmp_path: Path) -> None:
    client = _client(tmp_path)
    overrides = {
        "PATH_INFO": "/",
        "SCRIPT_NAME": "",
        "HTTP_HOST": "ao4eqbwn1l.execute-api.us-east-2.amazonaws.com",
        "awsgi.event": {"requestContext": {"stage": "prod"}},
    }
    response = client.get("/", environ_overrides=overrides)
    assert response.status_code == 200
    assert b'href="/prod/pricing"' in response.data

    pricing = client.get("/pricing", environ_overrides=overrides)
    assert pricing.status_code == 200


def test_custom_domain_does_not_add_stage_prefix(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/",
        environ_overrides={
            "PATH_INFO": "/",
            "SCRIPT_NAME": "",
            "HTTP_HOST": "hive-flow-ai.com",
            "awsgi.event": {"requestContext": {"stage": "prod"}},
        },
    )
    assert response.status_code == 200
    assert b'href="/pricing"' in response.data
    assert b'href="/prod/pricing"' not in response.data


def test_strips_stage_prefix_from_path_info(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get(
        "/",
        environ_overrides={
            "PATH_INFO": "/prod/pricing",
            "SCRIPT_NAME": "",
            "HTTP_HOST": "ao4eqbwn1l.execute-api.us-east-2.amazonaws.com",
            "awsgi.event": {"requestContext": {"stage": "prod"}},
        },
    )
    assert response.status_code == 200
    assert b"HiveFlowAI" in response.data or b"Hive Flow" in response.data


def test_static_serves_symbol(tmp_path: Path) -> None:
    client = _client(tmp_path)
    static = client.get("/static/hiveflowai-logo-mono.svg")
    assert static.status_code == 200
    assert static.mimetype == "image/svg+xml"
    assert b"<svg" in static.data


def test_awsgi_encodes_svg_for_api_gateway(tmp_path: Path) -> None:
    import base64

    import awsgi

    from hiveflow.dna.web.theme import BINARY_STATIC_CONTENT_TYPES

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    env_config = config["companies"]["poc"]["environments"]["dev"]
    app = create_app(settings, company="POC", environment="dev", env_config=env_config)

    event = {
        "httpMethod": "GET",
        "path": "/static/hiveflowai-logo-mono.svg",
        "headers": {"Accept": "image/svg+xml"},
        "queryStringParameters": None,
        "body": "",
        "isBase64Encoded": False,
        "requestContext": {"stage": "prod"},
    }
    result = awsgi.response(app, event, None, base64_content_types=BINARY_STATIC_CONTENT_TYPES)

    assert result["statusCode"] in (200, "200")
    assert result["isBase64Encoded"] is True
    assert result["headers"]["Content-Type"] == "image/svg+xml"
    decoded = base64.b64decode(result["body"])
    assert decoded[:4] == b"<svg"


def test_static_serves_echarts_bundle(tmp_path: Path) -> None:
    client = _client(tmp_path)
    static = client.get("/static/echarts.min.js")
    assert static.status_code == 200
    assert static.mimetype == "application/javascript"
    assert b"echarts" in static.data.lower()


def test_branding_ignores_legacy_s3_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The S3 branding override is retired; even with these env vars set (as a
    # stale deploy config might still have them), static assets must come from
    # the bundled files, never from S3.
    monkeypatch.setenv("HIVEFLOW_BRANDING_BUCKET", "hive-flow-ai-branding")
    monkeypatch.setenv("HIVEFLOW_BRANDING_SYMBOL_KEY", "HiveFlowAI Symbol.svg")

    client = _client(tmp_path)
    static = client.get("/static/hiveflowai-logo-mono.svg")
    assert static.status_code == 200
    assert static.mimetype == "image/svg+xml"
    assert b"<svg" in static.data
    assert static.data != b"fake-svg-bytes"


def test_web_app_api_endpoints(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    pack = client.get("/api/pack")
    assert pack.status_code == 200
    assert pack.json["pack_id"] == "poc_dna_config"

    revenue = client.get("/api/outputs/out_fact_revenue_lines?limit=500")
    assert revenue.status_code == 200
    assert revenue.json["output_id"] == "out_fact_revenue_lines"
    assert revenue.json["row_count"] == 0
    assert len(revenue.json["rows"]) <= REVENUE_TABLE_LIMIT

    kpis = client.get("/api/outputs/out_kpi_snapshot")
    assert kpis.status_code == 200
    assert kpis.json["output_id"] == "out_kpi_snapshot"

    trend = client.get("/api/reporting/pages/revenue-trend")
    assert trend.status_code == 200
    assert trend.json["page_id"] == "page_revenue_trend"
    assert trend.json["charts"][0]["data"]["series"] == []


def test_web_app_generic_reporting_api(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    output = client.get("/api/outputs/out_fact_revenue_lines?limit=10")
    assert output.status_code == 200
    assert output.json["output_id"] == "out_fact_revenue_lines"
    assert output.json["columns"]

    pages = client.get("/api/reporting/pages")
    assert pages.status_code == 200
    assert any(page["path"] == "/portal/revenue" for page in pages.json["pages"])

    page = client.get("/api/reporting/pages/revenue")
    assert page.status_code == 200
    assert page.json["page_id"] == "page_revenue"
    assert page.json["tables"][0]["data"]["rows"] == []

    catalog = client.get("/api/reporting/catalog")
    assert catalog.status_code == 200
    assert catalog.json["outputs"]


def test_aggregate_revenue_by_month() -> None:
    rows = [
        {"postingDate": "2026-01-15", "netAmount": 100.0},
        {"postingDate": "2026-01-20", "netAmount": 50.0},
        {"postingDate": "2026-02-01", "netAmount": 200.0},
        {"postingDate": "2025-11-01", "netAmount": 10.0},
    ]
    assert aggregate_revenue_by_month(rows, limit=0) == [
        ("2025-11", 10.0),
        ("2026-01", 150.0),
        ("2026-02", 200.0),
    ]
    assert aggregate_revenue_by_month(rows, limit=2) == [
        ("2026-01", 150.0),
        ("2026-02", 200.0),
    ]


def test_portal_revenue_trend_after_login(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    trend = client.get("/portal/revenue-trend")
    assert trend.status_code == 200
    assert b"Revenue trend" in trend.data
    assert b"No revenue trend yet" in trend.data
    assert b"data-hive-chart" not in trend.data
    assert b"portal-charts.js" not in trend.data


def test_portal_revenue_trend_with_data_includes_echarts(tmp_path: Path, portal_env: None) -> None:
    from hiveflow.ingest.storage import write_parquet_local

    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    write_parquet_local(
        tmp_path / "gold" / "dna" / "out_fact_revenue_lines",
        "data.parquet",
        [
            {"postingDate": "2026-01-15", "netAmount": 100.0},
            {"postingDate": "2026-02-01", "netAmount": 200.0},
        ],
    )

    trend = client.get("/portal/revenue-trend")
    assert trend.status_code == 200
    assert b'data-hive-chart="' in trend.data
    assert b"portal-charts.js" in trend.data
    assert b"echarts.min.js" in trend.data
    assert b"Monthly posted revenue" in trend.data


def test_portal_chart_demo_after_login(tmp_path: Path, chart_catalog_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    demo = client.get("/portal/chart-demo")
    assert demo.status_code == 200
    assert b"Chart catalog" in demo.data
    assert b"8 chart types" in demo.data
    assert b"Gold-backed demo" in demo.data
    assert demo.data.count(b"No gold data yet") == 8
    assert b"data-hive-chart=" not in demo.data
    assert b"portal-charts.js" not in demo.data


def test_portal_chart_demo_with_gold_data(tmp_path: Path, chart_catalog_env: None) -> None:
    from hiveflow.ingest.storage import write_parquet_local

    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    write_parquet_local(
        tmp_path / "gold" / "dna" / "out_fact_revenue_lines",
        "data.parquet",
        [
            {
                "postingDate": "2026-01-15",
                "netAmount": 100.0,
                "quantity": 2.0,
                "customerId": "c1",
                "customerName": "Acme",
                "itemId": "i1",
            },
            {
                "postingDate": "2026-02-01",
                "netAmount": 200.0,
                "quantity": 4.0,
                "customerId": "c1",
                "customerName": "Acme",
                "itemId": "i1",
            },
        ],
    )
    write_parquet_local(
        tmp_path / "gold" / "dna" / "out_dim_items",
        "data.parquet",
        [{"id": "i1", "displayName": "Widget A", "number": "W-A"}],
    )

    demo = client.get("/portal/chart-demo")
    assert demo.status_code == 200
    assert demo.data.count(b"data-hive-chart=") == 7
    assert b"No gold data yet" in demo.data
    assert b"portal-charts.js" in demo.data
    assert b"out_fact_revenue_lines" in demo.data
    assert b"Monthly posted revenue" in demo.data


def test_portal_chart_demo_disabled_without_flag(tmp_path: Path, portal_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from hiveflow.dna.web.portal import reporting_layout

    monkeypatch.setattr(reporting_layout, "chart_catalog_enabled", lambda _layout: False)
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    demo = client.get("/portal/chart-demo")
    assert demo.status_code == 404


def test_client_portal_config_from_yaml() -> None:
    from hiveflow.dna.web.portal.config import load_client_portal_config
    from hiveflow.project_config import get_platform_environment_config

    env_config = get_platform_environment_config("dev")
    client = load_client_portal_config("poc", env_config, default_pack_id="bc_intra_v1")
    assert client.display_name == "POC Distribution Co."
    assert client.pack_id == "bc_intra_v1"
    assert client.max_users == 10
    assert client.reporting_company == "poc"


def test_sanitize_portal_next_rewrites_reporting_login_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", ".hive-flow-ai.com")
    assert (
        _sanitize_portal_next("https://poc.hive-flow-ai.com/portal/login", client_id="poc")
        == "/portal"
    )
    assert _sanitize_portal_next("/portal/login") == "/portal"


def test_brand_home_href_uses_primary_site_on_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    from hiveflow.dna.web.theme import brand_home_href

    monkeypatch.setenv("HIVEFLOW_PRIMARY_SITE_URL", "https://hive-flow-ai.com")
    assert brand_home_href(lambda path: f"https://poc.hive-flow-ai.com{path}") == "https://hive-flow-ai.com/"


def test_portal_brand_links_to_summary() -> None:
    from types import SimpleNamespace

    from hiveflow.dna.web.theme import render_portal_page

    html = render_portal_page(
        title="Summary",
        active_path="/portal",
        body="<p>ok</p>",
        nav_links=(),
        client=SimpleNamespace(display_name="POC Distribution Co.", accent_color=None),
        url=lambda path: path,
    )
    assert 'class="brand" href="/portal"' in html


def test_reporting_portal_login_redirects_to_global_with_relative_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HIVEFLOW_GLOBAL_LOGIN_URL", "https://hive-flow-ai.com/portal/login")
    monkeypatch.setenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", ".hive-flow-ai.com")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
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
    response = client.get("/portal/login")
    assert response.status_code == 302
    assert response.headers["Location"] == (
        "https://hive-flow-ai.com/portal/login?next=%2Fportal&client_id=poc&client_id_locked=1"
    )


def test_portal_admin_users_requires_login(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    response = client.get("/portal/governance/users")
    assert response.status_code == 302
    assert "/portal/login" in response.headers["Location"]


def test_portal_admin_users_lists_legacy_users(tmp_path: Path, portal_env: None) -> None:
    client = _client(tmp_path)
    client.post("/portal/login", data={"username": "poc", "password": "changeme"})

    response = client.get("/portal/governance/users")
    assert response.status_code == 200
    assert b"Users" in response.data
    assert b"poc" in response.data
    assert b"1 of 10 seats used" in response.data
    assert b"Pack Registry" in response.data


def test_portal_admin_users_invite_post(tmp_path: Path, cognito_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    from hiveflow.dna.web.portal.auth import PortalUser
    from hiveflow.dna.web.portal.cognito import PortalLoginResult, PortalUserRecord

    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "")

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    client = Client(create_app(settings, company="POC", environment="dev", env_config=env_config))

    with patch(
        "hiveflow.dna.web.portal.cognito.authenticate_with_cognito",
        return_value=PortalLoginResult(
            kind="authenticated",
            user=PortalUser(username="poc", client_id="poc"),
        ),
    ), patch(
        "hiveflow.dna.web.portal.cognito.portal_user_is_admin",
        return_value=True,
    ), patch(
        "hiveflow.dna.web.portal.cognito.list_portal_users_for_client",
        return_value=[
            PortalUserRecord(
                username="poc",
                email="poc@example.com",
                client_id="poc",
                role="admin",
                status="CONFIRMED",
                enabled=True,
            )
        ],
    ), patch(
        "hiveflow.dna.web.portal.cognito.invite_portal_user",
        return_value={
            "username": "jane",
            "client_id": "poc",
            "email": "jane@example.com",
            "role": "member",
            "status": "FORCE_CHANGE_PASSWORD",
            "delivery": "invite_email",
        },
    ) as mock_invite:
        client.post("/portal/login", data={"action": "sign_in", "username": "poc", "password": "SecretPass123!"})
        response = client.post(
            "/portal/governance/users",
            data={
                "action": "invite",
                "username": "jane",
                "email": "jane@example.com",
                "role": "member",
            },
        )

    assert response.status_code == 200
    assert b"Invite sent to jane@example.com" in response.data
    mock_invite.assert_called_once()
    assert mock_invite.call_args.kwargs["client_id"] == "poc"
    assert mock_invite.call_args.kwargs["max_users"] == 10
    assert mock_invite.call_args.kwargs["role"] == "member"


def test_global_admin_can_access_fixed_client_reporting_portal(
    tmp_path: Path,
    cognito_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from hiveflow.dna.web.portal.auth import PortalUser
    from hiveflow.dna.web.portal.cognito import PortalLoginResult

    monkeypatch.setenv("HIVEFLOW_UI_MODE", "reporting")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")
    monkeypatch.setenv("HIVEFLOW_ADMIN_USERNAME", "GlobalAdmin")
    monkeypatch.setenv("HIVEFLOW_PORTAL_SESSION_SECRET", "test-global-admin-secret")

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    app = create_app(settings, company="POC", environment="dev", env_config=env_config, ui_mode="reporting")
    client = Client(app)

    with patch(
        "hiveflow.dna.web.portal.cognito.authenticate_with_cognito",
        return_value=PortalLoginResult(
            kind="authenticated",
            user=PortalUser(username="GlobalAdmin", client_id="platform"),
        ),
    ), patch(
        "hiveflow.dna.web.portal.cognito.portal_user_is_admin",
        return_value=True,
    ), patch(
        "hiveflow.dna.web.portal.cognito.list_portal_users_for_client",
        return_value=[],
    ):
        login = client.post(
            "/portal/login",
            data={
                "username": "GlobalAdmin",
                "password": "SecretPass123!",
                "client_id": "poc",
            },
        )
        assert login.status_code == 302

        response = client.get("/portal/governance/users")
        assert response.status_code == 200
        assert b"Users" in response.data


def test_client_user_cannot_access_other_client_reporting_portal(
    tmp_path: Path,
    cognito_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from hiveflow.dna.web.portal.auth import PortalSession, create_session_token

    monkeypatch.setenv("HIVEFLOW_UI_MODE", "reporting")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc2")
    monkeypatch.setenv("HIVEFLOW_PORTAL_SESSION_SECRET", "test-client-secret")

    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    app = create_app(settings, company="POC", environment="dev", env_config=env_config, ui_mode="reporting")
    client = Client(app)

    token = create_session_token(
        PortalSession(username="poc", client_id="poc", issued_at=int(time.time())),
        company="POC",
        environment="dev",
    )
    client.set_cookie("hiveflow_portal_session", token)

    response = client.get("/portal/governance/users")
    assert response.status_code == 302
    assert "/portal/login" in (response.headers.get("Location") or "")


def test_authorize_portal_client_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from hiveflow.dna.web.portal.auth import PortalClientAccessError, authorize_portal_client_access
    from hiveflow.project_config import get_platform_environment_config

    monkeypatch.setenv("HIVEFLOW_ADMIN_USERNAME", "GlobalAdmin")
    env_config = get_platform_environment_config("dev")

    assert (
        authorize_portal_client_access(
            username="GlobalAdmin",
            identity_client_id="platform",
            requested_client_id="poc",
            env_config=env_config,
        )
        == "poc"
    )

    with pytest.raises(PortalClientAccessError, match="Enter your client portal id"):
        authorize_portal_client_access(
            username="GlobalAdmin",
            identity_client_id="platform",
            requested_client_id="",
            env_config=env_config,
        )

    with pytest.raises(PortalClientAccessError, match="does not match your account"):
        authorize_portal_client_access(
            username="jane",
            identity_client_id="poc",
            requested_client_id="poc2",
            env_config=env_config,
        )

    assert (
        authorize_portal_client_access(
            username="jane",
            identity_client_id="poc",
            requested_client_id="poc",
            fixed_client_id="poc",
            env_config=env_config,
        )
        == "poc"
    )
