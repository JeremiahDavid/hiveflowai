"""Coverage for HIVEFLOW_UI_MODE=reporting_multitenant (PortalStack).

One Lambda serves every client; the tenant is resolved per request from the
session ``client_id`` against the config.yaml registry, never a pinned
``HIVEFLOW_PORTAL_CLIENT_ID``. Tenant resolution fails closed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from werkzeug.test import Client

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import _resolve_ui_mode, create_app
from hiveflow.dna.web.portal.auth import PortalSession, PortalTenantUnresolved, create_session_token
from hiveflow.dna.web.portal.config import ClientPortalConfig
from hiveflow.dna.web.portal.routes import _portal_settings
from hiveflow.dna.web.portal.tenant import resolve_tenant_dna_settings
from hiveflow.project_config import get_platform_environment_config, load_project_config

_SECRET = "test-multitenant-secret"


def _client_cfg(client_id: str, reporting_company: str) -> ClientPortalConfig:
    return ClientPortalConfig(
        client_id=client_id,
        display_name=client_id,
        welcome_title="",
        welcome_message="",
        reporting_company=reporting_company,
    )


def _base_settings(tmp_path: Path) -> DnaSettings:
    # Neutral base settings, as the shared Lambda builds them (no bucket/company).
    return DnaSettings(source="dbc", data_dir=tmp_path, company="", pack_id="")


def _multitenant_app(tmp_path: Path) -> Client:
    env_config = get_platform_environment_config("dev")
    return Client(
        create_app(
            _base_settings(tmp_path),
            company="poc",
            environment="dev",
            env_config=env_config,
            ui_mode="reporting_multitenant",
        )
    )


def _login_cookie(client: Client, *, username: str, client_id: str) -> None:
    token = create_session_token(
        PortalSession(username=username, client_id=client_id, issued_at=int(time.time())),
        company="poc",
        environment="dev",
    )
    client.set_cookie("hiveflow_portal_session", token)


# ── ui_mode plumbing ────────────────────────────────────────────────────────


def test_resolve_ui_mode_accepts_reporting_multitenant() -> None:
    assert _resolve_ui_mode("reporting_multitenant") == "reporting_multitenant"
    assert _resolve_ui_mode("bogus") == "full"


# ── _portal_settings fail-closed ────────────────────────────────────────────


def test_portal_settings_strict_raises_without_reporting_company(tmp_path: Path) -> None:
    with pytest.raises(PortalTenantUnresolved):
        _portal_settings(
            _base_settings(tmp_path),
            _client_cfg("ghost", reporting_company=""),
            environment="dev",
            strict=True,
        )


def test_portal_settings_non_strict_falls_back_to_base(tmp_path: Path) -> None:
    base = _base_settings(tmp_path)
    result = _portal_settings(
        base, _client_cfg("ghost", reporting_company=""), environment="dev", strict=False
    )
    assert result is base


def test_portal_settings_strict_binds_per_tenant_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def _fake_bucket(company, environment, **_kw):
        seen["company"] = company
        return f"meshflow-{company}-123-us-east-2"

    monkeypatch.setattr("hiveflow.project_config.resolve_data_bucket_name", _fake_bucket)

    poc = _portal_settings(
        _base_settings(tmp_path), _client_cfg("poc", "poc"), environment="dev", strict=True
    )
    assert poc.company == "poc"
    assert poc.s3_bucket == "meshflow-poc-123-us-east-2"

    poc2 = _portal_settings(
        _base_settings(tmp_path), _client_cfg("poc2", "poc2"), environment="dev", strict=True
    )
    assert poc2.company == "poc2"
    assert poc2.s3_bucket == "meshflow-poc2-123-us-east-2"


# ── resolve_tenant_dna_settings (worker / CFN path) ─────────────────────────


def test_resolve_tenant_dna_settings_binds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hiveflow.project_config.resolve_data_bucket_name",
        lambda company, environment, **_kw: f"meshflow-{company}-x",
    )
    settings = resolve_tenant_dna_settings("poc2", "dev")
    assert settings.company == "poc2"
    assert settings.s3_bucket == "meshflow-poc2-x"


def test_resolve_tenant_dna_settings_unknown_client_raises() -> None:
    with pytest.raises(PortalTenantUnresolved):
        resolve_tenant_dna_settings("does-not-exist", "dev")


# ── end-to-end: pinned env var is ignored, stale cookie is rejected ─────────


def test_multitenant_ignores_pinned_client_id_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("HIVEFLOW_PORTAL_CLIENT_ID", "poc2")  # must be ignored
    client = _multitenant_app(tmp_path)
    _login_cookie(client, username="alice", client_id="poc")

    response = client.get("/portal/executive")
    # The poc session is NOT bounced to login for a client mismatch the way a
    # pinned single-tenant Lambda would bounce it.
    assert not (
        response.status_code == 302
        and "/portal/login" in (response.headers.get("Location") or "")
    )


def test_multitenant_rejects_stale_cookie_for_unknown_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEFLOW_PORTAL_SESSION_SECRET", _SECRET)
    client = _multitenant_app(tmp_path)
    _login_cookie(client, username="ghost", client_id="ghostclient")

    response = client.get("/portal/executive")
    assert response.status_code == 302
    assert "/portal/login" in (response.headers.get("Location") or "")


def test_config_has_reporting_company_for_every_portal_client() -> None:
    from hiveflow.client_registry import iter_portal_clients_missing_reporting_company

    problems = iter_portal_clients_missing_reporting_company(load_project_config())
    assert problems == [], f"portal clients missing reporting_company: {problems}"


# ── Phase 3: per-tenant credential isolation (AssumeRole seam) ───────────────


@pytest.fixture
def _clear_tenant_cache():
    from hiveflow.dna.web.portal import tenant_credentials

    tenant_credentials._cache.clear()
    yield
    tenant_credentials._cache.clear()


class _FakeSts:
    def __init__(self) -> None:
        self.assumed: list[str] = []

    def get_caller_identity(self):
        return {"Account": "123456789012"}

    def assume_role(self, **kw):
        import datetime

        self.assumed.append(kw["RoleArn"])
        return {
            "Credentials": {
                "AccessKeyId": "AKIA_TEST",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=1),
            }
        }


def test_storage_aws_seam_prefers_injected_session() -> None:
    from hiveflow.storage.aws import current_boto_session, s3_client, use_boto_session

    sentinel = object()

    class _Sess:
        def client(self, service, **kw):
            assert service == "s3"
            return sentinel

    assert current_boto_session() is None
    with use_boto_session(_Sess()):
        assert current_boto_session() is not None
        assert s3_client() is sentinel
    assert current_boto_session() is None


def test_tenant_credentials_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch, _clear_tenant_cache
) -> None:
    monkeypatch.setenv("HIVEFLOW_TENANT_ASSUME_ROLE", "0")
    import boto3

    from hiveflow.dna.web.portal.tenant_credentials import tenant_credentials

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("boto3.client called while assume-role disabled")

    monkeypatch.setattr(boto3, "client", _boom)
    with tenant_credentials("poc", "dev"):
        pass


def test_tenant_credentials_assumes_expected_role(
    monkeypatch: pytest.MonkeyPatch, _clear_tenant_cache
) -> None:
    monkeypatch.setenv("HIVEFLOW_TENANT_ASSUME_ROLE", "1")
    import boto3

    fake = _FakeSts()
    monkeypatch.setattr(boto3, "client", lambda service, **kw: fake)

    from hiveflow.dna.web.portal.tenant_credentials import tenant_boto_session

    session = tenant_boto_session("poc2", "dev")
    assert session is not None
    assert fake.assumed == [
        "arn:aws:iam::123456789012:role/hiveflow-portal-tenant-poc2-dev"
    ]


def test_tenant_credentials_failure_raises(
    monkeypatch: pytest.MonkeyPatch, _clear_tenant_cache
) -> None:
    monkeypatch.setenv("HIVEFLOW_TENANT_ASSUME_ROLE", "1")
    import boto3

    from hiveflow.dna.web.portal.tenant_credentials import (
        TenantCredentialsError,
        tenant_credentials,
    )

    class _BrokenSts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

        def assume_role(self, **kw):
            raise RuntimeError("access denied")

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _BrokenSts())
    with pytest.raises(TenantCredentialsError):
        with tenant_credentials("poc", "dev"):
            pass


def test_kpi_worker_assumes_tenant_role(
    monkeypatch: pytest.MonkeyPatch, _clear_tenant_cache
) -> None:
    monkeypatch.setenv("HIVEFLOW_UI_MODE", "reporting_multitenant")
    monkeypatch.setenv("HIVEFLOW_TENANT_ASSUME_ROLE", "1")
    monkeypatch.setenv("HIVEFLOW_ENVIRONMENT", "dev")
    monkeypatch.setenv("HIVEFLOW_COMPANY", "poc")
    import boto3

    fake = _FakeSts()
    monkeypatch.setattr(boto3, "client", lambda service, **kw: fake)

    monkeypatch.setattr(
        "hiveflow.dna.web.portal.tenant.resolve_tenant_dna_settings",
        lambda client_id, environment: DnaSettings(
            source="dbc", data_dir=Path("/tmp"), company=client_id, pack_id=""
        ),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "hiveflow.dna.web.portal.kpi_generator.generation.run_kpi_generation_job",
        lambda settings, event: captured.update(company=settings.company) or {"ok": True},
    )

    from hiveflow.dna.web.lambda_handler import ui_handler

    result = ui_handler(
        {"hiveflow_task": "kpi_generator_generate", "client_id": "poc2"}, None
    )
    assert result == {"ok": True}
    assert captured["company"] == "poc2"
    assert fake.assumed == [
        "arn:aws:iam::123456789012:role/hiveflow-portal-tenant-poc2-dev"
    ]
