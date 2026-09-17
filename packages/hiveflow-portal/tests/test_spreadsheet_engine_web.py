"""Spreadsheet Engine web app: shared-portal-session auth path.

The Spreadsheet Engine is its own FastAPI app/Lambda but deliberately has no
login of its own — it trusts the same signed ``hiveflow_portal_session``
cookie the real portal issues (see ``hiveflow.spreadsheet_lab.web.auth``).
These tests only cover that trust boundary; tenant resolution
(``hiveflow.spreadsheet_lab.web.tenant``) is stubbed out since it needs a
real multi-tenant config.yaml/AWS setup covered elsewhere.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

from hiveflow.dna.web.portal.auth import PortalSession, create_session_token, session_cookie_name


@pytest.fixture(autouse=True)
def _stub_tenant_scope(monkeypatch: pytest.MonkeyPatch):
    from hiveflow.spreadsheet_lab.web import tenant as tenant_module

    @contextmanager
    def _fake_scope(client_id: str):
        yield "poc"

    monkeypatch.setattr(tenant_module, "tenant_scope", _fake_scope)
    monkeypatch.setattr("hiveflow.spreadsheet_lab.web.app.tenant_scope", _fake_scope)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIVEFLOW_PORTAL_SESSION_SECRET", "test-shared-secret")

    from hiveflow.spreadsheet_lab.web.app import create_spreadsheet_lab_app

    return TestClient(create_spreadsheet_lab_app())


def _session_cookie(*, username: str = "jane", client_id: str = "poc") -> str:
    import time

    session = PortalSession(username=username, client_id=client_id, issued_at=int(time.time()))
    return create_session_token(session, company="poc", environment="dev")


def test_no_session_redirects_to_portal_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/portal/login?next=")


def test_valid_shared_session_reaches_the_app(client: TestClient) -> None:
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert b"Upload a workbook" in response.content


def test_tampered_session_cookie_is_rejected(client: TestClient) -> None:
    token = _session_cookie()
    client.cookies.set(session_cookie_name(), token[:-1] + ("0" if token[-1] != "0" else "1"))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/portal/login?next=")
