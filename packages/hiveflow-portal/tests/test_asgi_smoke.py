"""Pass-1 FastAPI shell: native /healthz + Werkzeug app served via a2wsgi + Mangum."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.asgi import create_asgi_app
from hiveflow.project_config import get_environment_config, load_project_config


def _asgi(tmp_path: Path, *, ui_mode: str | None = None):
    settings = DnaSettings(source="dbc", data_dir=tmp_path, pack_id="bc_intra_v1")
    config = load_project_config()
    try:
        from hiveflow.project_config import get_platform_environment_config

        env_config = get_platform_environment_config("dev")
    except KeyError:
        env_config = config["companies"]["poc"]["environments"]["dev"]
    return create_asgi_app(
        settings, company="POC", environment="dev", env_config=env_config, ui_mode=ui_mode
    )


@pytest.fixture
def portal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_USERNAME", "poc")
    monkeypatch.setenv("HIVEFLOW_PORTAL_PASSWORD", "changeme")
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc")


def test_healthz_is_native(tmp_path: Path) -> None:
    client = TestClient(_asgi(tmp_path))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_public_landing_via_wsgi_fallback(tmp_path: Path) -> None:
    client = TestClient(_asgi(tmp_path))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "HiveFlowAI" in resp.text


def test_portal_login_via_wsgi_fallback(tmp_path: Path, portal_env: None) -> None:
    client = TestClient(_asgi(tmp_path))
    resp = client.get("/portal/login")
    assert resp.status_code == 200
    assert 'name="username"' in resp.text
    assert 'action' in resp.text


def test_static_theme_css_via_wsgi_fallback(tmp_path: Path) -> None:
    client = TestClient(_asgi(tmp_path))
    resp = client.get("/static/theme.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert len(resp.text) > 1000


def test_local_login_sets_cookie_and_portal_loads(tmp_path: Path, portal_env: None) -> None:
    client = TestClient(_asgi(tmp_path), follow_redirects=False)
    resp = client.post(
        "/portal/login",
        data={"action": "sign_in", "username": "poc", "password": "changeme",
              "client_id": "poc", "next": "/portal"},
    )
    assert resp.status_code in (302, 303)
    assert "hiveflow_portal_session" in resp.headers.get("set-cookie", "")


def test_portal_logout_is_native_and_clears_cookie(tmp_path: Path) -> None:
    client = TestClient(_asgi(tmp_path), follow_redirects=False)
    resp = client.get("/portal/logout")
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/portal/login")
    set_cookie = resp.headers.get("set-cookie", "")
    assert "hiveflow_portal_session=" in set_cookie
    assert ("Max-Age=0" in set_cookie) or ("expires=" in set_cookie.lower())


def test_stage_prefix_from_execute_api_event(tmp_path: Path) -> None:
    """A raw API Gateway proxy event with an execute-api Host -> links get /prod."""
    from mangum import Mangum

    handler = Mangum(_asgi(tmp_path), lifespan="off")
    event = {
        "version": "1.0",
        "httpMethod": "GET",
        "path": "/portal/login",
        "headers": {"Host": "abc123.execute-api.us-east-2.amazonaws.com"},
        "requestContext": {"stage": "prod", "httpMethod": "GET", "path": "/prod/portal/login"},
        "queryStringParameters": None,
        "body": None,
        "isBase64Encoded": False,
    }
    resp = handler(event, None)
    assert resp["statusCode"] == 200
    body = resp["body"]
    # generated links (form action, static hrefs) should carry the stage prefix
    assert "/prod/portal/login" in body or "/prod/static/" in body
