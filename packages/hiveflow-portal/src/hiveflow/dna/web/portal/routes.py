"""Client portal route registration — extracted from app.py (Phase 1 split).

Covers both HIVEFLOW_UI_MODE=global's thin login/logout/home stub (which hands
off to a client's reporting subdomain) and HIVEFLOW_UI_MODE=reporting's full
per-client portal app. That branch-by-ui_mode behavior inside e.g.
on_portal_home is intentional shared logic, not something this split
untangles further -- see docs/architecture.md and the Phase 1 plan.
"""

from __future__ import annotations

import contextlib
import logging
import os
from contextvars import ContextVar
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

from werkzeug.routing import Rule
from werkzeug.wrappers import Request, Response

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import load_pack_from_settings, read_json_artifact
from hiveflow.dna.web.portal.auth import (
    authenticate,
    authorize_portal_client_access,
    clear_session_cookie,
    client_id_from_reporting_hostname,
    effective_portal_client_id,
    is_global_portal_admin,
    is_global_portal_client_id,
    list_configured_portal_client_ids,
    login_response,
    normalize_portal_client_id,
    PortalClientAccessError,
    PortalTenantUnresolved,
    require_portal_admin,
    require_portal_session,
    resolve_login_client_id_hint,
    session_from_request,
)
from hiveflow.dna.web.portal.config import load_client_portal_config
from hiveflow.dna.web.portal.preview import clear_preview_cookie, preview_proposal_id
from hiveflow.dna.web.portal.reporting_layout import find_reporting_page
from hiveflow.dna.web.portal.reporting_api import (
    fetch_output_rows,
    fetch_page_data,
    list_reporting_pages_json,
)
from hiveflow.dna.web.portal.governance_helpers.gold_bindings import build_reporting_binding_catalog
from hiveflow.dna.web.portal.views import (
    _legacy_portal_users,
    render_admin_users,
    render_configured_page,
    render_governance,
)
from hiveflow.dna.web.theme import render_login_page
from hiveflow.dna.web.routing_helpers import _app_url, _json_response, _redirect

logger = logging.getLogger("hiveflow.portal.tenant")

# Per-request scope for tenant credential isolation. ``application()`` (app.py)
# opens ``request_tenant_scope()`` around dispatch; ``_portal_settings`` (strict)
# pushes the assumed-role session onto the ExitStack so every AWS client built
# for the rest of the request uses the tenant's credentials.
_request_tenant_stack: ContextVar[contextlib.ExitStack | None] = ContextVar(
    "hiveflow_request_tenant_stack", default=None
)
_request_tenant_bound: ContextVar[str] = ContextVar(
    "hiveflow_request_tenant_bound", default=""
)


@contextlib.contextmanager
def request_tenant_scope():
    """Open a per-request tenant-credential scope for the duration of dispatch."""
    stack = contextlib.ExitStack()
    tok_stack = _request_tenant_stack.set(stack)
    tok_bound = _request_tenant_bound.set("")
    try:
        with stack:
            yield
    finally:
        _request_tenant_bound.reset(tok_bound)
        _request_tenant_stack.reset(tok_stack)


def _bind_request_tenant_credentials(company: str, environment: str) -> None:
    """Assume ``company``'s data role for the rest of the current request.

    No-op outside an HTTP request scope (unit tests, async workers) — those
    callers install credentials themselves.
    """
    stack = _request_tenant_stack.get()
    if stack is None or not company:
        return
    if _request_tenant_bound.get() == company:
        return
    from hiveflow.dna.web.portal.tenant_credentials import tenant_credentials

    stack.enter_context(tenant_credentials(company, environment))
    _request_tenant_bound.set(company)


GLOBAL_UI_ENDPOINTS = frozenset(
    {
        "landing",
        "platform",
        "pricing",
        "portal_login",
        "portal_logout",
        "portal_home",
        "portal_admin_users",
        "static",
    }
)

REPORTING_UI_ENDPOINTS = frozenset(
    {
        "portal_login",
        "portal_logout",
        "portal_home",
        "portal_executive",
        "portal_revenue",
        "portal_revenue_trend",
        "portal_chart_demo",
        "portal_configured_page",
        "portal_catalog",
        "portal_catalog_output",
        "portal_catalog_gold",
        "portal_catalog_silver",
        "portal_catalog_silver_entity",
        "portal_dna",
        "portal_dna_kpi_generator",
        "portal_dna_kpi_generator_status",
        "portal_data_profile",
        "portal_data_profile_source_refresh",
        "portal_data_profile_entity",
        "portal_model_mapping",
        "portal_governance",
        "portal_governance_users",
        "portal_governance_config",
        "portal_governance_config_preview_exit",
        "portal_source_docs_inspector",
        "portal_source_docs_inspector_source",
        "portal_admin_users",
        "portal_admin_config",
        "portal_admin_config_preview_exit",
        "static",
        "api_pack",
        "api_manifest",
        "api_output",
        "api_reporting_pages",
        "api_reporting_page",
        "api_reporting_catalog",
        "api_source_docs_gold",
        "api_source_docs_gold_build",
        "api_source_docs_gold_exclude",
        "api_source_docs_gold_undo_exclude",
        "api_source_docs_gold_submit",
        "api_source_docs_gold_versions",
        "api_source_docs_gold_versions_commit",
        "api_source_docs_gold_restore",
        "api_spreadsheet_engine_status",
        "api_spreadsheet_engine_workbook",
    }
)


def _kpi_generator_redirect(
    request: Request,
    *,
    proposal_id: str = "",
    message: str = "",
    error: str = "",
) -> Response:
    """Post/Redirect/Get for KPI Generator; keep viewport on validation results."""
    params: dict[str, str] = {"validated": "1"}
    if proposal_id:
        params["proposal_id"] = proposal_id
    if message:
        params["msg"] = message
    if error:
        params["err"] = error
    return _redirect(
        request,
        f"/portal/dna/kpi-generator?{urlencode(params)}#kpi-generator-validation",
    )


def _portal_settings(
    base_settings: DnaSettings,
    client_config: Any,
    *,
    environment: str,
    strict: bool = False,
) -> DnaSettings:
    """Bind a request to its tenant's ``DnaSettings``.

    ``strict`` (multi-tenant mode): the tenant MUST resolve from
    ``client_config.reporting_company`` — never fall through to the Lambda's
    env-var base settings. A missing ``reporting_company`` or data bucket raises
    ``PortalTenantUnresolved`` (→ HTTP 403).
    """
    from hiveflow.project_config import (
        get_environment_config,
        resolve_aws_deploy_env,
        resolve_data_bucket_name,
        resolve_dna_source,
    )

    from hiveflow.storage.paths import company_dna_config_id

    reporting_company = str(getattr(client_config, "reporting_company", "")).strip()
    client_id = str(getattr(client_config, "client_id", "")).strip()
    company = reporting_company or base_settings.company
    pack_id = company_dna_config_id(company) if company else (
        client_config.pack_id or base_settings.pack_id
    )

    use_local_data = os.getenv("HIVEFLOW_LOCAL_DATA", "").strip().lower() in {"1", "true", "yes"}

    if strict and not reporting_company:
        logger.warning(
            "tenant_unresolved client_id=%s reason=no_reporting_company", client_id or "?"
        )
        raise PortalTenantUnresolved(
            f"Reporting is not configured for client {client_id or 'this account'}."
        )

    if reporting_company:
        try:
            client_env = get_environment_config(reporting_company, environment)
        except KeyError as exc:
            if strict:
                logger.error(
                    "tenant_unresolved client_id=%s company=%s reason=no_company_env",
                    client_id or "?",
                    reporting_company,
                )
                raise PortalTenantUnresolved(
                    f"Reporting company {reporting_company!r} is not configured for {environment}."
                ) from exc
            raise
        # Multi-tenant: always derive the bucket from the tenant, never inherit
        # a bucket baked into the shared Lambda's base settings.
        bucket = None if strict else base_settings.s3_bucket
        if not bucket and not use_local_data:
            try:
                account, region = resolve_aws_deploy_env(client_env, environment)
                bucket = resolve_data_bucket_name(
                    reporting_company,
                    environment,
                    account=account,
                    region=region,
                )
            except ValueError:
                bucket = None
        source = resolve_dna_source(client_env)
        if strict:
            if not bucket:
                logger.error(
                    "tenant_unresolved client_id=%s company=%s reason=no_data_bucket",
                    client_id or "?",
                    reporting_company,
                )
                raise PortalTenantUnresolved(
                    f"Data store is not provisioned for client {client_id or reporting_company}."
                )
            logger.info(
                "tenant_scope client_id=%s company=%s bucket=%s pack_id=%s",
                client_id or "?",
                reporting_company,
                bucket,
                pack_id,
            )
            # Run the rest of this request with the tenant's assumed credentials.
            _bind_request_tenant_credentials(reporting_company, environment)
        return DnaSettings(
            source=source,
            data_dir=base_settings.data_dir,
            s3_bucket=bucket,
            company=reporting_company,
            pack_id=pack_id,
            pack_version=base_settings.pack_version,
        )

    if pack_id != base_settings.pack_id or company != base_settings.company:
        return DnaSettings(
            source=base_settings.source,
            data_dir=base_settings.data_dir,
            s3_bucket=base_settings.s3_bucket,
            company=company or base_settings.company,
            pack_id=pack_id,
            pack_version=base_settings.pack_version,
        )
    return base_settings


def _client_reporting_site_url(client_id: str) -> str | None:
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip().lstrip(".")
    if not cookie_domain:
        return None
    normalized = client_id.strip().lower()
    if not normalized:
        return None
    return f"https://{normalized}.{cookie_domain}/portal"


def _client_id_from_reporting_hostname(url: str) -> str | None:
    return client_id_from_reporting_hostname(url)


def _sanitize_portal_next(next_path: str, *, client_id: str = "") -> str:
    """Normalize post-login targets — never bounce through reporting /portal/login."""
    value = (next_path or "/portal").strip()
    if not value:
        return "/portal"
    if value.rstrip("/") == "/portal/login":
        return "/portal"
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        path = parsed.path or "/"
        if path.rstrip("/") == "/portal/login":
            return "/portal"
        effective_client = client_id.strip().lower()
        if is_global_portal_client_id(effective_client):
            derived = _client_id_from_reporting_hostname(value)
            if derived:
                effective_client = derived
        reporting_base = _client_reporting_site_url(effective_client) if effective_client else None
        if reporting_base:
            base = urlparse(reporting_base)
            if parsed.netloc == base.netloc and path.startswith("/portal"):
                suffix = path.removeprefix("/portal")
                return f"/portal{suffix}" if suffix else "/portal"
    return value


def _external_redirect(url: str) -> Response:
    return Response(status=302, headers={"Location": url})


def build_portal_routes(
    settings: DnaSettings,
    *,
    company: str,
    environment: str,
    env_config: dict[str, Any],
    resolved_ui_mode: str,
    fixed_client_id: str,
    global_login_url: str,
) -> tuple[list[Rule], dict[str, Callable[..., Response]]]:
    """Build the client-portal Rule list and endpoint dispatch table."""

    # Multi-tenant: one Lambda serves every client, so tenant resolution must
    # fail closed rather than fall back to env-var base settings.
    tenant_strict = resolved_ui_mode == "reporting_multitenant"

    rules: list[Rule] = []
    if resolved_ui_mode in {"full", "global"}:
        rules.extend(
            [
                Rule("/portal/login", endpoint="portal_login", methods=["GET", "POST"]),
                Rule("/portal/logout", endpoint="portal_logout"),
                # Legacy Team URL → Governance Users
                Rule("/portal/admin/users", endpoint="portal_admin_users", methods=["GET", "POST"]),
            ]
        )
        if resolved_ui_mode == "global":
            rules.extend(
                [
                    Rule("/portal", endpoint="portal_home"),
                    Rule("/portal/", endpoint="portal_home"),
                ]
            )
    if resolved_ui_mode in {"full", "reporting", "reporting_multitenant"}:
        rules.extend(
            [
                Rule("/portal/login", endpoint="portal_login", methods=["GET", "POST"]),
                Rule("/portal/logout", endpoint="portal_logout"),
                Rule("/portal", endpoint="portal_home"),
                Rule("/portal/", endpoint="portal_home"),
                Rule("/portal/dna", endpoint="portal_dna"),
                Rule(
                    "/portal/dna/kpi-generator/status",
                    endpoint="portal_dna_kpi_generator_status",
                ),
                Rule(
                    "/portal/dna/kpi-generator",
                    endpoint="portal_dna_kpi_generator",
                    methods=["GET", "POST"],
                ),
                Rule("/portal/catalog/silver/<entity>", endpoint="portal_catalog_silver_entity"),
                Rule("/portal/catalog/silver", endpoint="portal_catalog_silver"),
                Rule("/portal/catalog/gold", endpoint="portal_catalog_gold"),
                Rule("/portal/catalog", endpoint="portal_catalog"),
                Rule("/portal/catalog/<output_id>", endpoint="portal_catalog_output"),
                Rule("/portal/dna/data-profile", endpoint="portal_data_profile"),
                Rule(
                    "/portal/dna/data-profile/<source>/refresh",
                    endpoint="portal_data_profile_source_refresh",
                    methods=["POST"],
                ),
                Rule(
                    "/portal/dna/data-profile/<source>/<entity>",
                    endpoint="portal_data_profile_entity",
                    methods=["GET", "POST"],
                ),
                Rule(
                    "/portal/dna/model-mapping",
                    endpoint="portal_model_mapping",
                    methods=["GET", "POST"],
                ),
                Rule("/portal/governance", endpoint="portal_governance", methods=["GET", "POST"]),
                Rule(
                    "/portal/governance/users",
                    endpoint="portal_governance_users",
                    methods=["GET", "POST"],
                ),
                Rule(
                    "/portal/governance/config",
                    endpoint="portal_governance_config",
                    methods=["GET", "POST"],
                ),
                Rule(
                    "/portal/governance/config/preview/exit",
                    endpoint="portal_governance_config_preview_exit",
                ),
                Rule("/portal/semantics/source-docs", endpoint="portal_source_docs_inspector"),
                Rule(
                    "/portal/semantics/source-docs/<source>",
                    endpoint="portal_source_docs_inspector_source",
                ),
                # Legacy admin URLs
                Rule("/portal/admin/users", endpoint="portal_admin_users", methods=["GET", "POST"]),
                Rule(
                    "/portal/admin/config",
                    endpoint="portal_admin_config",
                    methods=["GET", "POST"],
                ),
                Rule(
                    "/portal/admin/config/preview/exit",
                    endpoint="portal_admin_config_preview_exit",
                ),
                Rule("/portal/executive", endpoint="portal_executive"),
                Rule("/portal/revenue", endpoint="portal_revenue"),
                Rule("/portal/revenue-trend", endpoint="portal_revenue_trend"),
                Rule("/portal/chart-demo", endpoint="portal_chart_demo"),
                # Catch-all for additional pages declared in reporting config.
                Rule("/portal/<path:subpath>", endpoint="portal_configured_page"),
                Rule("/api/pack", endpoint="api_pack"),
                Rule("/api/manifest", endpoint="api_manifest"),
                Rule("/api/outputs/<output_id>", endpoint="api_output"),
                Rule("/api/reporting/pages", endpoint="api_reporting_pages"),
                Rule("/api/reporting/pages/<path:subpath>", endpoint="api_reporting_page"),
                Rule("/api/reporting/catalog", endpoint="api_reporting_catalog"),
                Rule("/api/source-docs-gold", endpoint="api_source_docs_gold"),
                Rule("/api/source-docs-gold/build", endpoint="api_source_docs_gold_build", methods=["POST"]),
                Rule("/api/source-docs-gold/exclude", endpoint="api_source_docs_gold_exclude", methods=["POST"]),
                Rule(
                    "/api/source-docs-gold/undo-exclude",
                    endpoint="api_source_docs_gold_undo_exclude",
                    methods=["POST"],
                ),
                Rule("/api/source-docs-gold/submit", endpoint="api_source_docs_gold_submit", methods=["POST"]),
                Rule("/api/source-docs-gold/versions", endpoint="api_source_docs_gold_versions"),
                Rule(
                    "/api/source-docs-gold/versions/commit",
                    endpoint="api_source_docs_gold_versions_commit",
                    methods=["POST"],
                ),
                Rule("/api/source-docs-gold/restore", endpoint="api_source_docs_gold_restore", methods=["POST"]),
                Rule("/api/spreadsheet-engine/status", endpoint="api_spreadsheet_engine_status"),
                Rule("/api/spreadsheet-engine/workbook", endpoint="api_spreadsheet_engine_workbook"),
            ]
        )

    def _client_config(client_id: str):
        if fixed_client_id:
            client_id = fixed_client_id
        ui_cfg = env_config.get("ui", {})
        default_pack_id = str(ui_cfg.get("pack_id", settings.pack_id))
        return load_client_portal_config(
            client_id,
            env_config,
            default_pack_id=default_pack_id,
        )

    def _portal_client_id(session) -> str:
        return effective_portal_client_id(session, fixed_client_id=fixed_client_id)

    def _login_url(request: Request) -> str:
        if global_login_url:
            return global_login_url
        return _app_url(request, "/portal/login")

    def _post_login_redirect(request: Request, user_client_id: str, next_path: str) -> str:
        next_path = _sanitize_portal_next(next_path, client_id=user_client_id)
        if next_path.startswith("http://") or next_path.startswith("https://"):
            return next_path
        if resolved_ui_mode == "global":
            reporting_base = _client_reporting_site_url(user_client_id)
            if reporting_base and next_path.startswith("/portal"):
                suffix = next_path.removeprefix("/portal")
                return f"{reporting_base.rstrip('/')}{suffix or ''}"
        return _app_url(request, next_path)

    def _login_client_id_context(request: Request, *, next_path: str) -> tuple[str, bool]:
        locked = str(request.values.get("client_id_locked", "")).strip().lower() in {"1", "true", "yes"}
        return resolve_login_client_id_hint(
            query_client_id=str(request.values.get("client_id", "")),
            next_path=next_path,
            fixed_client_id=fixed_client_id,
            locked=locked,
        )

    def _render_login_form(
        request: Request,
        *,
        url,
        next_path: str,
        mode: str = "sign_in",
        error: str = "",
        success: str = "",
        username: str = "",
        session_token: str = "",
        client_id: str | None = None,
        client_id_locked: bool | None = None,
    ) -> Response:
        if client_id is None or client_id_locked is None:
            resolved_client_id, resolved_locked = _login_client_id_context(request, next_path=next_path)
            client_id = resolved_client_id if client_id is None else client_id
            client_id_locked = resolved_locked if client_id_locked is None else client_id_locked
        response = Response(
            render_login_page(
                url=url,
                next_path=next_path,
                mode=mode,
                error=error,
                success=success,
                username=username,
                session=session_token,
                client_id=client_id,
                client_id_locked=client_id_locked,
            ),
            mimetype="text/html",
        )
        if error and mode in {"sign_in", "set_password"}:
            response.status_code = 401
        return response

    def _complete_portal_login(
        request: Request,
        user,
        *,
        requested_client_id: str,
        next_path: str,
        url,
        mode: str = "sign_in",
        username: str = "",
        session_token: str = "",
    ) -> Response:
        try:
            portal_client_id = authorize_portal_client_access(
                username=user.username,
                identity_client_id=user.client_id,
                requested_client_id=requested_client_id,
                fixed_client_id=fixed_client_id,
                env_config=env_config,
            )
        except PortalClientAccessError as exc:
            client_id, client_id_locked = _login_client_id_context(request, next_path=next_path)
            return _render_login_form(
                request,
                url=url,
                next_path=next_path,
                mode=mode,
                error=exc.message,
                username=username,
                session_token=session_token,
                client_id=client_id,
                client_id_locked=client_id_locked,
            )
        return login_response(
            user,
            company=company,
            environment=environment,
            portal_client_id=portal_client_id,
            redirect_to=_post_login_redirect(request, portal_client_id, next_path),
        )

    def on_portal_login(request: Request) -> Response:
        if resolved_ui_mode in {"reporting", "reporting_multitenant"} and global_login_url:
            existing = session_from_request(request, company=company, environment=environment)
            if existing is not None:
                next_path = _sanitize_portal_next(
                    request.args.get("next", "/portal"),
                    client_id=existing.client_id,
                )
                destination = _post_login_redirect(request, existing.client_id, next_path)
                if destination.startswith("http://") or destination.startswith("https://"):
                    return _external_redirect(destination)
                return _redirect(request, next_path)
            # ``reporting`` is pinned to one client via ``fixed_client_id``;
            # ``reporting_multitenant`` serves every client, so the hint comes
            # from the reporting subdomain the browser arrived on.
            login_client_hint = fixed_client_id
            if resolved_ui_mode == "reporting_multitenant":
                login_client_hint = (
                    _client_id_from_reporting_hostname(f"https://{request.host}/") or ""
                )
            next_path = _sanitize_portal_next(
                request.args.get("next", "/portal"),
                client_id=login_client_hint,
            )
            params = {"next": next_path}
            if login_client_hint:
                params["client_id"] = login_client_hint
                params["client_id_locked"] = "1"
            return _external_redirect(f"{global_login_url}?{urlencode(params)}")

        from hiveflow.dna.web.portal.cognito import (
            PasswordResetError,
            authenticate_with_cognito,
            cognito_configured,
            complete_new_password_challenge,
            confirm_password_reset,
            request_password_reset,
        )

        url = lambda path: _app_url(request, path)
        login_modes = {"sign_in", "forgot_password", "reset_password", "set_password"}

        if request.method == "GET":
            existing = session_from_request(request, company=company, environment=environment)
            next_path = _sanitize_portal_next(
                request.args.get("next", "/portal"),
                client_id=existing.client_id if existing is not None else str(request.args.get("client_id", "")),
            )
            if existing is not None:
                destination = _post_login_redirect(request, existing.client_id, next_path)
                if destination.startswith("http://") or destination.startswith("https://"):
                    return _external_redirect(destination)
                return Response(status=302, headers={"Location": destination})
            mode = request.args.get("mode", "sign_in")
            if mode not in login_modes or mode == "set_password":
                mode = "sign_in"
            return _render_login_form(request, url=url, next_path=next_path, mode=mode)

        action = request.form.get("action", "sign_in")
        next_path = request.form.get("next", "/portal") or "/portal"
        requested_client_id = str(request.form.get("client_id", "")).strip()

        if action == "forgot_password":
            username = request.form.get("username", "")
            if not cognito_configured():
                return Response(
                    render_login_page(
                        url=url,
                        error="Password reset is only available for Cognito-managed portal accounts.",
                        next_path=next_path,
                        mode="forgot_password",
                        username=username,
                    ),
                    mimetype="text/html",
                    status=400,
                )
            try:
                request_password_reset(username, company=company, environment=environment)
            except PasswordResetError as exc:
                return Response(
                    render_login_page(
                        url=url,
                        error=exc.message,
                        next_path=next_path,
                        mode="forgot_password",
                        username=username,
                    ),
                    mimetype="text/html",
                    status=400,
                )
            return Response(
                render_login_page(
                    url=url,
                    success="If an account exists for that username, we sent a reset code to the email on file.",
                    next_path=next_path,
                    mode="reset_password",
                    username=username,
                ),
                mimetype="text/html",
            )

        if action == "confirm_forgot_password":
            username = request.form.get("username", "")
            confirmation_code = request.form.get("confirmation_code", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if new_password != confirm_password:
                return Response(
                    render_login_page(
                        url=url,
                        error="Passwords do not match.",
                        next_path=next_path,
                        mode="reset_password",
                        username=username,
                    ),
                    mimetype="text/html",
                    status=400,
                )
            if not cognito_configured():
                return Response(
                    render_login_page(
                        url=url,
                        error="Password reset is only available for Cognito-managed portal accounts.",
                        next_path=next_path,
                        mode="reset_password",
                        username=username,
                    ),
                    mimetype="text/html",
                    status=400,
                )
            try:
                confirm_password_reset(
                    username=username,
                    confirmation_code=confirmation_code,
                    new_password=new_password,
                    company=company,
                    environment=environment,
                )
            except PasswordResetError as exc:
                return Response(
                    render_login_page(
                        url=url,
                        error=exc.message,
                        next_path=next_path,
                        mode="reset_password",
                        username=username,
                    ),
                    mimetype="text/html",
                    status=400,
                )
            return Response(
                render_login_page(
                    url=url,
                    success="Password updated. Sign in with your new password.",
                    next_path=next_path,
                    mode="sign_in",
                ),
                mimetype="text/html",
            )

        if action == "set_password":
            username = request.form.get("username", "")
            session = request.form.get("session", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if new_password != confirm_password:
                return Response(
                    render_login_page(
                        url=url,
                        error="Passwords do not match.",
                        next_path=next_path,
                        mode="set_password",
                        username=username,
                        session=session,
                    ),
                    mimetype="text/html",
                    status=400,
                )
            user = complete_new_password_challenge(
                username=username,
                session=session,
                new_password=new_password,
                company=company,
                environment=environment,
            )
            if user is None:
                return _render_login_form(
                    request,
                    url=url,
                    next_path=next_path,
                    mode="set_password",
                    error="Could not update your password. Check the policy and try again.",
                    username=username,
                    session_token=session,
                    client_id=requested_client_id,
                )
            return _complete_portal_login(
                request,
                user,
                requested_client_id=requested_client_id,
                next_path=next_path,
                url=url,
                mode="set_password",
                username=username,
                session_token=session,
            )

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if cognito_configured():
            login_result = authenticate_with_cognito(
                username,
                password,
                company=company,
                environment=environment,
            )
            if login_result is None:
                return _render_login_form(
                    request,
                    url=url,
                    next_path=next_path,
                    error="Invalid username or password.",
                    username=username,
                    client_id=requested_client_id,
                )
            if login_result.kind == "new_password" and login_result.challenge is not None:
                challenge = login_result.challenge
                return _render_login_form(
                    request,
                    url=url,
                    next_path=next_path,
                    mode="set_password",
                    username=challenge.username,
                    session_token=challenge.session,
                    client_id=requested_client_id,
                )
            if login_result.user is None:
                return _render_login_form(
                    request,
                    url=url,
                    next_path=next_path,
                    error="Invalid username or password.",
                    username=username,
                    client_id=requested_client_id,
                )
            return _complete_portal_login(
                request,
                login_result.user,
                requested_client_id=requested_client_id,
                next_path=next_path,
                url=url,
                username=username,
            )

        user = authenticate(username, password, company=company, environment=environment)
        if user is None:
            return _render_login_form(
                request,
                url=url,
                next_path=next_path,
                error="Invalid username or password.",
                username=username,
                client_id=requested_client_id,
            )
        return _complete_portal_login(
            request,
            user,
            requested_client_id=requested_client_id,
            next_path=next_path,
            url=url,
            username=username,
        )

    def on_portal_logout(request: Request) -> Response:
        response = _redirect(request, "/portal/login")
        clear_session_cookie(response)
        return response

    def _authorized(request: Request):
        login_url = _login_url(request)
        session, redirect = require_portal_session(
            request,
            company=company,
            environment=environment,
            login_url=login_url,
        )
        if session is not None and fixed_client_id and session.client_id != fixed_client_id:
            return None, _redirect(request, "/portal/login")
        if session is not None and tenant_strict:
            client_id = normalize_portal_client_id(session.client_id)
            known = client_id in list_configured_portal_client_ids(env_config)
            if not known and not is_global_portal_admin(
                username=session.username, client_id=client_id
            ):
                # Stale cookie for a removed/renamed tenant — force re-auth.
                logger.warning(
                    "tenant_session_rejected client_id=%s username=%s",
                    client_id or "?",
                    session.username,
                )
                return None, _redirect(request, "/portal/login")
        return session, redirect

    def _portal_is_admin(username: str) -> bool:
        return require_portal_admin(username, company=company, environment=environment)

    def _resolve_reporting_override(
        request: Request,
        *,
        portal_settings: DnaSettings,
        is_admin: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not is_admin:
            return None, None
        _ = preview_proposal_id(request)
        return None, None

    def _render_reporting_path(request: Request, path: str) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        is_admin = _portal_is_admin(session.username)
        reporting_override, preview_meta = _resolve_reporting_override(
            request, portal_settings=portal_settings, is_admin=is_admin
        )
        page = find_reporting_page(portal_settings, path, override=reporting_override)
        if page is None:
            return Response("Report page is not configured for this client.", status=404, mimetype="text/plain")
        return render_configured_page(
            request,
            settings=portal_settings,
            client=client,
            page=page,
            is_admin=is_admin,
            reporting_override=reporting_override,
            preview_meta=preview_meta,
        )

    def on_portal_home(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        if resolved_ui_mode == "global":
            reporting_url = _client_reporting_site_url(_portal_client_id(session))
            if reporting_url:
                return _external_redirect(reporting_url)
            return Response(
                "Reporting dashboard URL is not configured for this client.",
                status=503,
                mimetype="text/plain",
            )
        return _render_reporting_path(request, "/portal")

    def on_portal_executive(request: Request) -> Response:
        return _render_reporting_path(request, "/portal/executive")

    def on_portal_revenue(request: Request) -> Response:
        return _render_reporting_path(request, "/portal/revenue")

    def on_portal_revenue_trend(request: Request) -> Response:
        return _render_reporting_path(request, "/portal/revenue-trend")

    def on_portal_chart_demo(request: Request) -> Response:
        return _render_reporting_path(request, "/portal/chart-demo")

    def on_portal_configured_page(request: Request, subpath: str) -> Response:
        reserved = {"login", "logout", "catalog", "governance", "semantics", "dna", "admin", "api"}
        first = (subpath or "").split("/", 1)[0].strip().lower()
        if first in reserved:
            return Response("Not found", status=404, mimetype="text/plain")
        return _render_reporting_path(request, f"/portal/{subpath}")

    def on_portal_dna(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_dna

        client = _client_config(_portal_client_id(session))
        return render_dna(
            request,
            settings=settings,
            client=client,
            is_admin=_portal_is_admin(session.username),
        )


    def on_portal_catalog(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_catalog

        client = _client_config(_portal_client_id(session))
        return render_catalog(
            request,
            settings=settings,
            client=client,
            is_admin=_portal_is_admin(session.username),
        )

    def on_portal_catalog_gold(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_catalog_gold

        client = _client_config(_portal_client_id(session))
        return render_catalog_gold(
            request,
            settings=settings,
            client=client,
            is_admin=_portal_is_admin(session.username),
        )

    def on_portal_catalog_silver(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_catalog_silver

        client = _client_config(_portal_client_id(session))
        return render_catalog_silver(
            request,
            settings=settings,
            client=client,
            is_admin=_portal_is_admin(session.username),
        )

    def on_portal_catalog_silver_entity(request: Request, entity: str) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_catalog_silver

        client = _client_config(_portal_client_id(session))
        return render_catalog_silver(
            request,
            settings=settings,
            client=client,
            entity=entity,
            is_admin=_portal_is_admin(session.username),
        )

    def on_portal_catalog_output(request: Request, output_id: str) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_catalog_table

        client = _client_config(_portal_client_id(session))
        return render_catalog_table(
            request,
            settings=settings,
            client=client,
            output_id=output_id,
            is_admin=_portal_is_admin(session.username),
        )

    def on_portal_data_profile(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_data_profile_index

        client = _client_config(_portal_client_id(session))
        return render_data_profile_index(
            request,
            settings=settings,
            client=client,
            is_admin=_portal_is_admin(session.username),
            configured_sources=_configured_reference_sources(settings),
        )

    def on_portal_data_profile_source_refresh(request: Request, source: str) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        if not _portal_is_admin(session.username):
            return Response("Forbidden", status=403, mimetype="text/plain")
        from hiveflow.dna.web.portal.data_profile_ui.service import refresh_source

        try:
            result = refresh_source(settings, source)
            params = {"msg": f"Re-profiled {result.get('profiled_count', 0)} table(s) in {source}."}
        except Exception as exc:  # noqa: BLE001
            params = {"err": str(exc)}
        return _redirect(request, f"/portal/dna/data-profile?{urlencode(params)}")

    def on_portal_data_profile_entity(request: Request, source: str, entity: str) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_data_profile_detail

        client = _client_config(_portal_client_id(session))
        is_admin = _portal_is_admin(session.username)

        if request.method == "POST":
            if not is_admin:
                return Response("Forbidden", status=403, mimetype="text/plain")
            from hiveflow.dna.web.portal.data_profile_ui.service import refresh_entity, save_overrides

            action = str(request.form.get("action") or "").strip()
            try:
                if action == "refresh_entity":
                    refresh_entity(settings, source, entity)
                    params = {"msg": "Re-ran Bedrock for this table."}
                elif action == "save_overrides":
                    purpose = request.form.get("purpose")
                    field_descriptions = {
                        key[len("description__") :]: value
                        for key, value in request.form.items()
                        if key.startswith("description__")
                    }
                    save_overrides(
                        settings,
                        source,
                        entity,
                        purpose=str(purpose) if purpose is not None else None,
                        field_descriptions=field_descriptions,
                    )
                    params = {"msg": "Saved manual overrides."}
                else:
                    params = {"err": f"Unknown action {action!r}"}
            except ValueError as exc:
                params = {"err": str(exc)}
            return _redirect(
                request, f"/portal/dna/data-profile/{source}/{entity}?{urlencode(params)}"
            )

        return render_data_profile_detail(
            request,
            settings=settings,
            client=client,
            source=source,
            entity=entity,
            is_admin=is_admin,
            message=str(request.args.get("msg") or ""),
            error=str(request.args.get("err") or ""),
        )

    def on_portal_model_mapping(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.web.portal.views import render_model_mapping

        client = _client_config(_portal_client_id(session))
        is_admin = _portal_is_admin(session.username)
        entity_param = str(request.args.get("entity") or "").strip()

        if request.method == "POST":
            if not is_admin:
                return Response("Forbidden", status=403, mimetype="text/plain")
            from hiveflow.dna.web.portal.model_mapping import service as mapping_service

            action = str(request.form.get("action") or "").strip()
            entity_id = str(request.form.get("entity_id") or "").strip()
            params: dict[str, str] = {}
            try:
                if action == "init":
                    mapping_service.initialize(
                        settings, str(request.form.get("industry_pack_id") or "").strip()
                    )
                    params = {"msg": "Mapping initialized from industry template."}
                elif action == "approve_field":
                    mapping_service.approve_field(
                        settings,
                        entity_id=entity_id,
                        field_id=str(request.form.get("field_id") or "").strip(),
                        silver_column=str(request.form.get("silver_column") or "").strip() or None,
                    )
                    params = {"msg": "Field approved.", "entity": entity_id}
                elif action == "reject_field":
                    mapping_service.reject_field(
                        settings,
                        entity_id=entity_id,
                        field_id=str(request.form.get("field_id") or "").strip(),
                    )
                    params = {"msg": "Field rejected.", "entity": entity_id}
                elif action == "approve_all":
                    mapping_service.approve_all(settings, entity_id=entity_id or None)
                    params = {"msg": "Approved all suggested fields.", "entity": entity_id}
                elif action == "reject_all":
                    mapping_service.reject_all(settings, entity_id=entity_id or None)
                    params = {"msg": "Rejected all fields.", "entity": entity_id}
                elif action == "exclude_entity":
                    mapping_service.exclude_entity(settings, entity_id=entity_id)
                    params = {"msg": f"Excluded entity {entity_id}."}
                elif action == "exclude_field":
                    mapping_service.exclude_field(
                        settings,
                        entity_id=entity_id,
                        field_id=str(request.form.get("field_id") or "").strip(),
                    )
                    params = {"msg": "Field removed.", "entity": entity_id}
                elif action == "add_entity":
                    mapping_service.add_custom_entity(
                        settings,
                        entity_id=entity_id,
                        silver_source=str(request.form.get("silver_source") or "").strip(),
                        silver_entity=str(request.form.get("silver_entity") or "").strip(),
                        field_id=str(request.form.get("field_id") or "").strip(),
                        silver_column=str(request.form.get("silver_column") or "").strip(),
                    )
                    params = {"msg": "Custom entity added."}
                elif action == "add_field":
                    mapping_service.add_custom_field(
                        settings,
                        entity_id=entity_id,
                        field_id=str(request.form.get("field_id") or "").strip(),
                        silver_column=str(request.form.get("silver_column") or "").strip(),
                    )
                    params = {"msg": "Custom field added.", "entity": entity_id}
                elif action == "promote":
                    result = mapping_service.promote(
                        settings, version=str(request.form.get("version") or "").strip()
                    )
                    report = result["report"]
                    included = ", ".join(report["included_kpis"]) or "none"
                    skipped = "; ".join(
                        f"{item['kpi_id']} ({item['reason']})" for item in report["skipped_kpis"]
                    ) or "none"
                    params = {
                        "msg": (
                            f"Promoted {result['pack'].pack_id} v{result['pack'].version} — "
                            f"included: {included} — skipped: {skipped}"
                        )
                    }
                else:
                    params = {"err": f"Unknown action {action!r}"}
            except ValueError as exc:
                params = {"err": str(exc)}
                if entity_id:
                    params["entity"] = entity_id
            return _redirect(request, f"/portal/dna/model-mapping?{urlencode(params)}")

        return render_model_mapping(
            request,
            settings=settings,
            client=client,
            is_admin=is_admin,
            entity=entity_param,
            message=str(request.args.get("msg") or ""),
            error=str(request.args.get("err") or ""),
        )

    def on_portal_governance_config(request: Request) -> Response:
        return _redirect(request, "/portal/governance")

    def on_portal_governance_config_preview_exit(request: Request) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        if not _portal_is_admin(session.username):
            return Response("Forbidden", status=403, mimetype="text/plain")
        response = _redirect(request, "/portal/governance")
        clear_preview_cookie(response)
        return response

    def on_portal_admin_config(request: Request) -> Response:
        return _redirect(request, "/portal/governance")

    def on_portal_admin_config_preview_exit(request: Request) -> Response:
        return _redirect(request, "/portal/governance/config/preview/exit")

    def on_portal_dna_kpi_generator(request: Request) -> Response:
        from hiveflow.dna.web.portal.dna_manual_refresh import (
            gold_refresh_status,
            quota_summary as manual_refresh_quota_summary,
            trigger_manual_refresh,
        )
        from hiveflow.dna.web.portal.governance_helpers.bedrock_usage import BedrockBudgetExceeded
        from hiveflow.dna.web.portal.kpi_generator.catalog import (
            parse_validation_filters,
            validation_criteria_from_proposal,
        )
        from hiveflow.dna.web.portal.kpi_generator.generation import (
            close_working_kpi_proposals,
            enqueue_kpi_generation,
            load_kpi_generator_workspace,
            load_kpi_proposal,
        )
        from hiveflow.dna.web.portal.kpi_generator.governance import (
            approve_all_kpi_drafts,
            approve_kpi_draft_group,
            approve_kpi_proposal,
            discard_kpi_proposal,
            publish_all_approved_kpis,
            reject_all_kpi_drafts,
            reject_kpi_draft_group,
            reject_kpi_proposal,
            run_validation,
            save_kpi_governance_draft,
            save_validation_criteria,
            update_kpi_draft_sql,
            validate_kpi_draft_group,
        )
        from hiveflow.dna.web.portal.kpi_generator.drafts import proposal_generation_status
        from hiveflow.dna.web.portal.views import render_kpi_generator
        from hiveflow.dna.workflow import load_production_pack, load_workflow_state

        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        is_admin = _portal_is_admin(session.username)
        message = str(request.args.get("msg") or "")
        error = str(request.args.get("err") or "")
        active_tab = str(request.args.get("tab") or "generator").strip().lower()
        if active_tab not in {"generator", "review"}:
            active_tab = "generator"
        proposal = None
        validation = None
        pending_drafts: list = []
        approved_drafts: list = []
        proposal_id = str(request.args.get("proposal_id") or "").strip()
        workspace_working = None
        if is_admin and request.method != "POST":
            workspace_working, pending_drafts, approved_drafts = load_kpi_generator_workspace(
                portal_settings
            )
        if proposal_id:
            loaded = load_kpi_proposal(portal_settings, proposal_id)
            if loaded and str(loaded.get("status") or "").strip().lower() == "working":
                proposal = loaded
        elif request.method != "POST" and is_admin:
            proposal = workspace_working
            if proposal:
                proposal_id = str(proposal.get("proposal_id") or "").strip()
        if proposal_generation_status(proposal) == "error":
            error = error or str(proposal.get("generation_error") or "KPI generation failed.")

        if request.method == "POST":
            if not is_admin:
                return Response("Forbidden", status=403, mimetype="text/plain")
            action = str(request.form.get("action", "")).strip()
            try:
                if action == "generate":
                    prior_proposal_id = str(request.form.get("prior_proposal_id") or "").strip()
                    prior_chat_history: list[dict[str, str]] | None = None
                    prior_validation_criteria = None
                    if prior_proposal_id:
                        prior = load_kpi_proposal(portal_settings, prior_proposal_id)
                        if prior and str(prior.get("status") or "").strip().lower() == "working":
                            prior_chat_history = prior.get("chat_history") or []
                            prior_validation_criteria = validation_criteria_from_proposal(prior)
                    proposal = enqueue_kpi_generation(
                        portal_settings,
                        prompt=str(request.form.get("prompt") or ""),
                        client_id=client.client_id,
                        monthly_budget_usd=client.config_assistant_monthly_budget_usd,
                        username=session.username,
                        prior_chat_history=prior_chat_history,
                        prior_validation_criteria=prior_validation_criteria,
                        prior_proposal_id=prior_proposal_id,
                    )
                    params = {"proposal_id": proposal["proposal_id"]}
                    if proposal_generation_status(proposal) != "pending":
                        params["msg"] = (
                            "Draft generated. Validate, then save as a DNA draft for review."
                        )
                    return _redirect(
                        request,
                        f"/portal/dna/kpi-generator?{urlencode(params)}",
                    )
                elif action == "validate":
                    proposal_id = str(request.form.get("proposal_id") or "").strip()
                    try:
                        sql = str(request.form.get("sql") or "").strip()
                        sql_by_layer = {
                            "silver": str(request.form.get("sql_silver") or "").strip(),
                            "gold": str(request.form.get("sql_gold") or "").strip(),
                        }
                        if any(sql_by_layer.values()) or sql:
                            update_kpi_draft_sql(
                                portal_settings,
                                proposal_id=proposal_id,
                                sql=sql,
                                sql_by_layer=sql_by_layer,
                            )
                        filters = parse_validation_filters(
                            request.form.getlist("filter_fact"),
                            request.form.getlist("filter_field"),
                            request.form.getlist("filter_value"),
                        )
                        save_validation_criteria(
                            portal_settings,
                            proposal_id=proposal_id,
                            filters=filters,
                        )
                        run_validation(
                            portal_settings,
                            proposal_id=proposal_id,
                            filters=filters,
                            company=portal_settings.company,
                            environment=environment,
                        )
                        return _kpi_generator_redirect(
                            request,
                            proposal_id=proposal_id,
                            message="Validation query completed.",
                        )
                    except BedrockBudgetExceeded as exc:
                        return _kpi_generator_redirect(
                            request,
                            proposal_id=proposal_id,
                            error=(
                                f"Monthly Bedrock allowance reached "
                                f"(${exc.estimated_cost_usd:.2f} / ${exc.monthly_budget_usd:.2f})."
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001
                        if proposal_id:
                            try:
                                filters = parse_validation_filters(
                                    request.form.getlist("filter_fact"),
                                    request.form.getlist("filter_field"),
                                    request.form.getlist("filter_value"),
                                )
                                save_validation_criteria(
                                    portal_settings,
                                    proposal_id=proposal_id,
                                    filters=filters,
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        return _kpi_generator_redirect(
                            request,
                            proposal_id=proposal_id,
                            error=str(exc),
                        )
                elif action == "save_draft":
                    proposal_id = str(request.form.get("proposal_id") or "").strip()
                    sql = str(request.form.get("sql") or "").strip()
                    sql_by_layer = {
                        "silver": str(request.form.get("sql_silver") or "").strip(),
                        "gold": str(request.form.get("sql_gold") or "").strip(),
                    }
                    if any(sql_by_layer.values()) or sql:
                        update_kpi_draft_sql(
                            portal_settings,
                            proposal_id=proposal_id,
                            sql=sql,
                            sql_by_layer=sql_by_layer,
                        )
                    result = save_kpi_governance_draft(
                        portal_settings,
                        proposal_id=proposal_id,
                        username=session.username,
                    )
                    message = (
                        f"Saved DNA draft v{result['version']} ({result['sql_file']}). "
                        "Review it on the Review Drafts tab."
                    )
                    close_working_kpi_proposals(
                        portal_settings,
                        username=session.username,
                    )
                    return _redirect(
                        request,
                        f"/portal/dna/kpi-generator?{urlencode({'tab': 'review', 'msg': message})}",
                    )
                elif action == "discard_draft":
                    proposal_id = str(request.form.get("proposal_id") or "").strip()
                    if proposal_id:
                        discard_kpi_proposal(
                            portal_settings,
                            proposal_id=proposal_id,
                            username=session.username,
                        )
                    close_working_kpi_proposals(
                        portal_settings,
                        username=session.username,
                    )
                    return _redirect(
                        request,
                        f"/portal/dna/kpi-generator?{urlencode({'msg': 'Draft discarded.'})}",
                    )
                elif action == "validate_integrity":
                    target_key = str(request.form.get("target_key") or "").strip()
                    proposal_ids = [
                        str(pid).strip()
                        for pid in request.form.getlist("proposal_ids")
                        if str(pid).strip()
                    ]
                    validation = validate_kpi_draft_group(
                        portal_settings,
                        target_key=target_key,
                        proposal_ids=proposal_ids,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    if str(validation.get("status") or "").strip().lower() == "passed":
                        message = f"Integrity validation passed for {target_key}."
                    else:
                        errors = validation.get("errors") or ["Integrity validation failed"]
                        error = "; ".join(str(err) for err in errors)
                    active_tab = "review"
                elif action == "approve_group":
                    target_key = str(request.form.get("target_key") or "").strip()
                    proposal_ids = [
                        str(pid).strip()
                        for pid in request.form.getlist("proposal_ids")
                        if str(pid).strip()
                    ]
                    next_version = str(request.form.get("next_sql_version") or "").strip() or None
                    result = approve_kpi_draft_group(
                        portal_settings,
                        target_key=target_key,
                        proposal_ids=proposal_ids,
                        username=session.username,
                        version=next_version,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    message = (
                        f"Approved {len(result.get('approved') or [])} draft(s) in group "
                        f"{target_key}. Move to Publish to materialize tables."
                    )
                    active_tab = "review"
                elif action == "publish_approved":
                    result = publish_all_approved_kpis(
                        portal_settings,
                        client_id=client.client_id,
                        username=session.username,
                        company=portal_settings.company,
                        environment=environment,
                        monthly_limit=client.dna_manual_refresh_monthly_limit,
                    )
                    published_count = len(result.get("published") or [])
                    message = (
                        f"Started DNA refresh for {published_count} approved KPI(s)."
                    )
                    active_tab = "review"
                elif action == "reject_group":
                    target_key = str(request.form.get("target_key") or "").strip()
                    proposal_ids = [
                        str(pid).strip()
                        for pid in request.form.getlist("proposal_ids")
                        if str(pid).strip()
                    ]
                    result = reject_kpi_draft_group(
                        portal_settings,
                        target_key=target_key,
                        proposal_ids=proposal_ids,
                        username=session.username,
                    )
                    message = (
                        f"Rejected {len(result.get('rejected') or [])} draft(s) in group {target_key}."
                    )
                    active_tab = "review"
                elif action == "approve":
                    proposal_id = str(request.form.get("proposal_id") or "").strip()
                    next_version = str(request.form.get("next_sql_version") or "").strip() or None
                    result = approve_kpi_proposal(
                        portal_settings,
                        proposal_id=proposal_id,
                        username=session.username,
                        version=next_version,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    message = (
                        f"Approved and pinned SQL pack v{result['version']} "
                        f"({result['sql_file']}). Publish from Review Drafts to materialize."
                    )
                    proposal = load_kpi_proposal(portal_settings, proposal_id)
                    active_tab = "review"
                elif action == "reject":
                    proposal_id = str(request.form.get("proposal_id") or "").strip()
                    result = reject_kpi_proposal(
                        portal_settings,
                        proposal_id=proposal_id,
                        username=session.username,
                    )
                    if str(result.get("prior_status") or "") == "approved":
                        message = (
                            f"Removed {proposal_id} from the ready-to-publish queue."
                        )
                    else:
                        message = f"Rejected draft {proposal_id}."
                    active_tab = "review"
                elif action == "approve_all":
                    results = approve_all_kpi_drafts(
                        portal_settings,
                        username=session.username,
                    )
                    message = f"Approved {len(results)} KPI draft(s) to production."
                    active_tab = "review"
                elif action == "reject_all":
                    results = reject_all_kpi_drafts(
                        portal_settings,
                        username=session.username,
                    )
                    message = f"Rejected {len(results)} KPI draft(s)."
                    active_tab = "review"
                elif action == "manual_dna_refresh":
                    workflow = load_workflow_state(portal_settings, portal_settings.dna_config_id)
                    pinned_version = str(workflow.get("active_version") or "").strip()
                    if not pinned_version:
                        try:
                            pinned_version = str(
                                load_production_pack(portal_settings).version or ""
                            ).strip()
                        except Exception:  # noqa: BLE001
                            pinned_version = ""
                    if not pinned_version:
                        raise ValueError("No production DNA version is pinned yet.")
                    reporting_company = str(client.reporting_company or "").strip() or company
                    result = trigger_manual_refresh(
                        portal_settings,
                        client_id=client.client_id,
                        username=session.username,
                        pinned_version=pinned_version,
                        company=reporting_company,
                        environment=environment,
                        monthly_limit=client.dna_manual_refresh_monthly_limit,
                    )
                    remaining = int((result.get("quota") or {}).get("remaining") or 0)
                    message = (
                        "DNA refresh started. Silver and gold tables will update when the run "
                        f"completes. {remaining} manual refresh(es) remaining this month."
                    )
                else:
                    error = f"Unknown action {action!r}"
            except BedrockBudgetExceeded as exc:
                error = (
                    f"Monthly Bedrock allowance reached "
                    f"(${exc.estimated_cost_usd:.2f} / ${exc.monthly_budget_usd:.2f})."
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            if is_admin:
                _, pending_drafts, approved_drafts = load_kpi_generator_workspace(
                    portal_settings
                )

        refresh_status = None
        refresh_quota = None
        if is_admin:
            workflow = load_workflow_state(portal_settings, portal_settings.dna_config_id)
            pinned_version = str(workflow.get("active_version") or "").strip()
            if not pinned_version:
                try:
                    pinned_version = str(load_production_pack(portal_settings).version or "").strip()
                except Exception:  # noqa: BLE001
                    pinned_version = ""
            refresh_status = gold_refresh_status(
                portal_settings,
                pinned_version=pinned_version,
            ).to_dict()
            refresh_quota = manual_refresh_quota_summary(
                portal_settings,
                client_id=client.client_id,
                monthly_limit=client.dna_manual_refresh_monthly_limit,
            ).to_dict()

        return render_kpi_generator(
            request,
            settings=portal_settings,
            client=client,
            is_admin=is_admin,
            proposal=proposal,
            validation=validation,
            message=message,
            error=error,
            active_tab=active_tab,
            pending_drafts=pending_drafts,
            approved_drafts=approved_drafts,
            refresh_status=refresh_status,
            refresh_quota=refresh_quota,
        )


    def on_portal_dna_kpi_generator_status(request: Request) -> Response:
        from hiveflow.dna.web.portal.kpi_generator.drafts import proposal_generation_status
        from hiveflow.dna.web.portal.kpi_generator.generation import load_kpi_proposal

        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        proposal_id = str(request.args.get("proposal_id") or "").strip()
        if not proposal_id:
            return _json_response({"error": "proposal_id required"}, status=400)
        proposal = load_kpi_proposal(portal_settings, proposal_id) or {}
        gen_status = proposal_generation_status(proposal) or "complete"
        return _json_response(
            {
                "proposal_id": proposal_id,
                "generation_status": gen_status,
                "error": str(proposal.get("generation_error") or ""),
            }
        )


    def on_portal_governance(request: Request) -> Response:
        from hiveflow.dna.web.portal.governance_restore import restore_governance_target
        from hiveflow.dna.web.portal.views import render_governance

        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        is_admin = _portal_is_admin(session.username)
        message = str(request.args.get("msg") or "")
        error = str(request.args.get("err") or "")

        if request.method == "POST":
            if not is_admin:
                return Response("Forbidden", status=403, mimetype="text/plain")
            action = str(request.form.get("action", "")).strip()
            try:
                if action in {"restore_dna", "restore_reporting"}:
                    target = "dna" if action == "restore_dna" else "reporting"
                    source_version = str(request.form.get("source_version", "")).strip()
                    result = restore_governance_target(
                        portal_settings,
                        target=target,
                        source_version=source_version,
                        username=session.username,
                    )
                    label = "DNA" if target == "dna" else "reporting"
                    message = (
                        f"Restored {label} from v{result['restored_from']} "
                        f"as v{result['version']} and pinned production. "
                        "Gold outputs are unchanged until compile and publish."
                    )
                    return _redirect(
                        request,
                        f"/portal/governance?{urlencode({'msg': message})}",
                    )
                raise ValueError(f"Unknown action {action!r}")
            except Exception as exc:  # noqa: BLE001 — surface governance errors in UI
                error = str(exc)

        return render_governance(
            request,
            settings=portal_settings,
            client=client,
            is_admin=is_admin,
            message=message,
            error=error,
        )

    def on_portal_governance_users(request: Request) -> Response:
        from hiveflow.dna.web.portal.cognito import (
            PORTAL_ROLE_MEMBER,
            PortalUserAlreadyExists,
            PortalUserLimitExceeded,
            cognito_configured,
            invite_portal_user,
            list_portal_users_for_client,
            set_portal_user_role,
        )

        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        if not _portal_is_admin(session.username):
            return Response(
                "Admin access required for Users. "
                "Your Cognito user needs custom:portal_role=admin.",
                status=403,
                mimetype="text/plain",
            )

        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        message = ""
        error = ""
        invites_enabled = cognito_configured()

        if request.method == "POST":
            action = str(request.form.get("action", "")).strip()
            if action == "invite":
                if not invites_enabled:
                    error = "User invites require Cognito authentication."
                else:
                    username = request.form.get("username", "").strip()
                    email = request.form.get("email", "").strip()
                    role = request.form.get("role", PORTAL_ROLE_MEMBER).strip()
                    try:
                        invite_portal_user(
                            username=username,
                            client_id=_portal_client_id(session),
                            email=email,
                            company=company,
                            environment=environment,
                            max_users=client.max_users,
                            role=role,
                        )
                        message = f"Invite sent to {email} as {role}."
                    except PortalUserLimitExceeded:
                        error = f"Seat limit reached ({client.max_users} users)."
                    except PortalUserAlreadyExists:
                        error = f"Username {username!r} is already taken."
                    except ValueError as exc:
                        error = str(exc)
                    except RuntimeError as exc:
                        error = str(exc)
            elif action == "set_role":
                if not invites_enabled:
                    error = "Role changes require Cognito authentication."
                else:
                    username = request.form.get("username", "").strip()
                    role = request.form.get("role", PORTAL_ROLE_MEMBER).strip()
                    if username.casefold() == session.username.strip().casefold():
                        error = "You cannot change your own role."
                    else:
                        try:
                            set_portal_user_role(
                                username=username,
                                role=role,
                                company=company,
                                environment=environment,
                            )
                            message = f"Updated {username} to {role}."
                        except ValueError as exc:
                            error = str(exc)
                        except RuntimeError as exc:
                            error = str(exc)

        if invites_enabled:
            users = list_portal_users_for_client(
                client_id=_portal_client_id(session),
                company=company,
                environment=environment,
            )
        else:
            users = _legacy_portal_users(_portal_client_id(session), company=company, environment=environment)

        return render_admin_users(
            request,
            client=client,
            users=users,
            current_username=session.username,
            message=message,
            error=error,
            invites_enabled=invites_enabled,
            is_admin=True,
            settings=portal_settings,
        )

    def on_portal_admin_users(request: Request) -> Response:
        if resolved_ui_mode == "global":
            session, redirect = _authorized(request)
            if redirect is not None:
                return redirect
            reporting_url = _client_reporting_site_url(_portal_client_id(session))
            if reporting_url:
                return _external_redirect(f"{reporting_url.rstrip('/')}/governance/users")
        return _redirect(request, "/portal/governance/users")


    def on_portal_source_docs_inspector(request: Request) -> Response:
        return _render_source_docs_inspector(request, source=None)

    def on_portal_source_docs_inspector_source(request: Request, source: str) -> Response:
        return _render_source_docs_inspector(request, source=source)

    def _configured_reference_sources(portal_settings) -> list[str]:
        from hiveflow.project_config import get_environment_config, iter_configured_connectors

        try:
            env_cfg = get_environment_config(portal_settings.company, environment)
        except Exception:  # noqa: BLE001
            return [portal_settings.source]
        return [name for name, _cfg in iter_configured_connectors(env_cfg)]

    def _render_source_docs_inspector(request: Request, *, source: str | None) -> Response:
        session, redirect = _authorized(request)
        if redirect is not None:
            return redirect
        from hiveflow.dna.source_docs.reference import normalize_reference_source
        from hiveflow.dna.web.portal.views import render_source_docs_inspector, render_spreadsheet_engine

        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        is_admin = _portal_is_admin(session.username)
        configured_sources = _configured_reference_sources(portal_settings)
        active = normalize_reference_source(source or "") or "sse"
        message = str(request.args.get("msg") or "")
        error = str(request.args.get("err") or "")

        if active == "sse" and request.method == "POST":
            if not is_admin:
                return Response("Forbidden", status=403, mimetype="text/plain")
            action = str(request.form.get("action") or "").strip()
            from hiveflow.dna.web.portal.governance_helpers.bedrock_usage import BedrockBudgetExceeded
            from hiveflow.dna.web.portal.spreadsheet_engine.service import (
                approve_clean_shape,
                approve_joins,
                approve_table,
                approve_transformation,
                chat_feedback,
                edit_transformation,
                complete_reload,
                enqueue_analysis,
                job_status,
                list_proposal_jobs,
                load_job_report,
                link_job_catalog,
                parse_upload,
                refresh_joins,
                reject_clean_shape,
                reject_joins,
                reject_job,
                reject_table,
                reject_transformation,
                reupload_to_catalog,
                request_schema_rewrite,
                request_transformation_rewrite,
                select_sheets,
                start_upload,
            )

            try:
                if action == "upload":
                    upload = request.files.get("workbook")
                    if upload is None or not upload.filename:
                        raise ValueError("Choose an Excel workbook (.xlsx) to upload.")
                    filename = upload.filename
                    if not filename.lower().endswith(".xlsx"):
                        raise ValueError("Only .xlsx workbooks are supported in this release.")
                    body = upload.read()
                    if not body:
                        raise ValueError("Uploaded file is empty.")
                    job = start_upload(
                        portal_settings,
                        filename=filename,
                        body=body,
                        username=session.username,
                        linked_catalog_id=str(request.form.get("linked_catalog_id") or "").strip(),
                    )
                    linked = str(request.form.get("linked_catalog_id") or "").strip()
                    parsed = parse_upload(portal_settings, job_id=job["job_id"])
                    if linked or str(parsed.get("status") or "") != "awaiting_sheets":
                        result = enqueue_analysis(
                            portal_settings,
                            job_id=job["job_id"],
                            company=portal_settings.company,
                            environment=environment,
                        )
                        params = {
                            "job_id": job["job_id"],
                            "tab": "review",
                            "table_index": "0",
                            "msg": (
                                "Workbook analyzed — review proposed tables."
                                if str(result.get("status") or "") != "enqueued"
                                else "Workbook uploaded — analysis started. Proposals will appear here when ready."
                            ),
                        }
                    else:
                        params = {
                            "job_id": job["job_id"],
                            "tab": "review",
                            "msg": "Workbook uploaded — select which sheets to analyze.",
                        }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "select_sheets":
                    job_id = str(request.form.get("job_id") or "").strip()
                    sheets = [str(item) for item in request.form.getlist("sheet") if str(item).strip()]
                    result = select_sheets(
                        portal_settings,
                        job_id=job_id,
                        sheets=sheets,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": "0",
                        "msg": (
                            "Generating cleaned proposals for the selected sheets."
                            if str(result.get("status") or "") == "enqueued"
                            else "Selected sheets analyzed — review proposed tables."
                        ),
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "chat":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    chat_feedback(
                        portal_settings,
                        job_id=job_id,
                        message=str(request.form.get("message") or ""),
                        table_id=table_id,
                        client_id=client.client_id,
                        monthly_budget_usd=client.config_assistant_monthly_budget_usd,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Assistant updated the proposal.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "approve_table":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    approve_table(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        username=session.username,
                    )
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode({
                            'job_id': job_id,
                            'tab': 'review',
                            'table_index': str(request.form.get('table_index') or '0'),
                            'msg': 'Table catalogued. Review DNA join proposals next.',
                        })}",
                    )
                if action == "approve_joins":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    approve_joins(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        selected_ids=list(request.form.getlist("join_id")),
                        username=session.username,
                    )
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode({
                            'job_id': job_id,
                            'tab': 'review',
                            'table_index': str(request.form.get('table_index') or '0'),
                            'msg': 'Lake joins approved.',
                        })}",
                    )
                if action == "refresh_joins":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    refresh_joins(portal_settings, job_id=job_id, table_id=table_id)
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode({
                            'job_id': job_id,
                            'tab': 'review',
                            'table_index': str(request.form.get('table_index') or '0'),
                            'msg': 'DNA join proposals refreshed.',
                        })}",
                    )
                if action == "reject_joins":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    reject_joins(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        reason=str(request.form.get("reason") or ""),
                        username=session.username,
                    )
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode({
                            'job_id': job_id,
                            'tab': 'review',
                            'table_index': str(request.form.get('table_index') or '0'),
                            'msg': 'Join proposals rejected — DNA re-ran from grain and keys.',
                        })}",
                    )
                if action == "approve_transformation":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    approve_transformation(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        username=session.username,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Transform output approved and saved for reuse.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "approve_clean_shape":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    approve_clean_shape(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        username=session.username,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Cleaned data approved — compare deterministic output next.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "reject_clean_shape":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    reject_clean_shape(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        reason=str(request.form.get("reason") or ""),
                        username=session.username,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Cleaned data rejected — describe what to fix in chat.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "reject_transformation":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    reject_transformation(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        reason=str(request.form.get("reason") or ""),
                        username=session.username,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Transform rejected — describe what to fix in chat.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "reject_table":
                    from hiveflow.spreadsheet.jobs import active_proposal_tables

                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    reject_table(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        username=session.username,
                    )
                    remaining = active_proposal_tables(
                        (load_job_report(portal_settings, job_id=job_id) or {}).get("tables") or []
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": "0",
                        "msg": "Table removed from this review.",
                    }
                    if remaining:
                        params["table_index"] = "0"
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "reject_job":
                    job_id = str(request.form.get("job_id") or "").strip()
                    reject_job(
                        portal_settings,
                        job_id=job_id,
                        username=session.username,
                    )
                    remaining = list_proposal_jobs(portal_settings)
                    next_job_id = ""
                    for item in remaining:
                        cid = str(item.get("job_id") or "")
                        if cid and cid != job_id:
                            next_job_id = cid
                            break
                    params = {
                        "tab": "review",
                        "msg": "Workbook removed from this review.",
                    }
                    if next_job_id:
                        params["job_id"] = next_job_id
                        params["table_index"] = "0"
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "edit_transformation":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    raw = str(request.form.get("transformation_json") or "").strip()
                    transformation = json.loads(raw) if raw else {}
                    edit_transformation(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        transformation=transformation,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Transformation updated — review and approve.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "link_catalog":
                    job_id = str(request.form.get("job_id") or "").strip()
                    catalog_id = str(request.form.get("catalog_id") or "").strip()
                    link_job_catalog(
                        portal_settings,
                        job_id=job_id,
                        catalog_id=catalog_id,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "msg": "Linked to catalog entry.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "reupload_catalog":
                    catalog_id = str(request.form.get("catalog_id") or "").strip()
                    upload = request.files.get("workbook")
                    if upload is None or not upload.filename:
                        raise ValueError("Choose an Excel workbook (.xlsx) to upload.")
                    filename = upload.filename
                    if not filename.lower().endswith(".xlsx"):
                        raise ValueError("Only .xlsx workbooks are supported in this release.")
                    body = upload.read()
                    if not body:
                        raise ValueError("Uploaded file is empty.")
                    result = reupload_to_catalog(
                        portal_settings,
                        catalog_id=catalog_id,
                        filename=filename,
                        body=body,
                        username=session.username,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    job_id = str((result.get("job") or {}).get("job_id") or "")
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "msg": (
                            "Workbook analyzed — review proposed tables."
                            if str(result.get("status") or "") != "enqueued"
                            else "Workbook uploaded — analysis started."
                        ),
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "complete_reload":
                    job_id = str(request.form.get("job_id") or "").strip()
                    table_id = str(request.form.get("table_id") or "").strip()
                    complete_reload(
                        portal_settings,
                        job_id=job_id,
                        table_id=table_id,
                        username=session.username,
                    )
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode({
                            'job_id': job_id,
                            'tab': 'review',
                            'table_index': str(request.form.get('table_index') or '0'),
                            'msg': 'Reload catalogued. Review DNA join proposals next.',
                        })}",
                    )
                if action == "request_schema_rewrite":
                    job_id = str(request.form.get("job_id") or "").strip()
                    request_schema_rewrite(
                        portal_settings,
                        job_id=job_id,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "Schema rewrite started with AI — review new proposals.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
                if action == "request_transformation_rewrite":
                    job_id = str(request.form.get("job_id") or "").strip()
                    request_transformation_rewrite(
                        portal_settings,
                        job_id=job_id,
                        company=portal_settings.company,
                        environment=environment,
                    )
                    params = {
                        "job_id": job_id,
                        "tab": "review",
                        "table_index": str(request.form.get("table_index") or "0"),
                        "msg": "New transformation proposed with AI — review and approve.",
                    }
                    return _redirect(
                        request,
                        f"/portal/semantics/source-docs/sse?{urlencode(params)}",
                    )
            except BedrockBudgetExceeded as exc:
                error = (
                    f"Monthly Bedrock allowance reached "
                    f"(${exc.estimated_cost_usd:.2f} / ${exc.monthly_budget_usd:.2f})."
                )
            except Exception as exc:  # noqa: BLE001
                error = str(exc)

        if active == "sse":
            job = None
            report = None
            job_id = str(request.args.get("job_id") or "").strip()
            from hiveflow.dna.web.portal.spreadsheet_engine.service import (
                job_status as load_job_status,
                list_proposal_jobs,
                load_job_report,
            )

            if not job_id:
                remaining = list_proposal_jobs(portal_settings)
                if remaining:
                    job_id = str((remaining[0] or {}).get("job_id") or "").strip()
            if job_id:
                status_payload = load_job_status(
                    portal_settings,
                    job_id=job_id,
                    company=portal_settings.company,
                    environment=environment,
                )
                job = status_payload.get("job")
                report = status_payload.get("report")
                if not report or not (report.get("tables") or []):
                    report = load_job_report(portal_settings, job_id=job_id)
            return render_spreadsheet_engine(
                request,
                settings=portal_settings,
                client=client,
                is_admin=is_admin,
                message=message,
                error=error,
                configured_sources=configured_sources,
                job=job if isinstance(job, dict) else None,
                report=report if isinstance(report, dict) else None,
            )

        return render_source_docs_inspector(
            request,
            settings=portal_settings,
            client=client,
            is_admin=is_admin,
            message=message,
            error=error,
            source=source,
            configured_sources=configured_sources,
        )


    def _semantics_portal_settings(request: Request) -> tuple[DnaSettings, Any, Response | None]:
        if (failure := _api_authorized(request)) is not None:
            return settings, None, failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        return portal_settings, session, None


    def on_api_source_docs_gold(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_gold_status

        source = str(request.args.get("source") or "").strip() or None
        return _json_response(source_docs_gold_status(portal_settings, source=source))

    def on_api_source_docs_gold_build(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        from hiveflow.dna.web.portal.semantics.source_docs_service import (
            enqueue_source_docs_gold_build,
        )

        body = request.get_json(silent=True) or {}
        source = str(body.get("source") or request.args.get("source") or "").strip() or None
        try:
            result = enqueue_source_docs_gold_build(
                portal_settings,
                company=company,
                environment=environment,
                source=source,
                seed_missing_overlays=bool(body.get("seed_missing_overlays", True)),
                publish_schemas=bool(body.get("publish_schemas", False)),
            )
            status_code = 200
            if result.get("status") == "error":
                status_code = 500
            return _json_response(result, status=status_code)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"error": str(exc)}, status=500)

    def on_api_source_docs_gold_exclude(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_exclude

        body = request.get_json(silent=True) or {}
        try:
            return _json_response(source_docs_exclude(portal_settings, body))
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"error": str(exc)}, status=500)

    def on_api_source_docs_gold_undo_exclude(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_undo_exclude

        body = request.get_json(silent=True) or {}
        try:
            return _json_response(source_docs_undo_exclude(portal_settings, body))
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"error": str(exc)}, status=500)

    def on_api_source_docs_gold_submit(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_submit_changes

        body = request.get_json(silent=True) or {}
        source = str(body.get("source") or request.args.get("source") or "").strip() or None
        raw_excludes = body.get("excludes")
        excludes = raw_excludes if isinstance(raw_excludes, list) else None
        try:
            result = source_docs_submit_changes(
                portal_settings,
                company=company,
                environment=environment,
                source=source,
                excludes=excludes,
            )
            status_code = 200
            if result.get("status") == "error":
                status_code = 400 if result.get("reason") == "no_pending" else 500
            return _json_response(result, status=status_code)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"error": str(exc)}, status=500)

    def on_api_source_docs_gold_versions(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_versions

        source = str(request.args.get("source") or "").strip() or None
        return _json_response(source_docs_versions(portal_settings, source=source))

    def on_api_source_docs_gold_versions_commit(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_commit_version

        body = request.get_json(silent=True) or {}
        source = str(body.get("source") or request.args.get("source") or "").strip() or None
        note = str(body.get("note") or "Submitted").strip() or "Submitted"
        try:
            return _json_response(
                source_docs_commit_version(portal_settings, source=source, note=note)
            )
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"error": str(exc)}, status=500)

    def on_api_source_docs_gold_restore(request: Request) -> Response:
        portal_settings, session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        if not _portal_is_admin(session.username):
            return _json_response({"error": "forbidden"}, status=403)
        from hiveflow.dna.web.portal.semantics.source_docs_service import source_docs_restore_version

        body = request.get_json(silent=True) or {}
        source = str(body.get("source") or request.args.get("source") or "").strip() or None
        try:
            version = int(body.get("version"))
        except (TypeError, ValueError):
            return _json_response({"error": "version must be an integer"}, status=400)
        try:
            return _json_response(
                source_docs_restore_version(portal_settings, version=version, source=source)
            )
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return _json_response({"error": str(exc)}, status=500)

    def on_api_spreadsheet_engine_status(request: Request) -> Response:
        portal_settings, _session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        from hiveflow.dna.web.portal.spreadsheet_engine.service import job_status

        job_id = str(request.args.get("job_id") or "").strip()
        if not job_id:
            return _json_response({"error": "job_id is required"}, status=400)
        payload = job_status(
            portal_settings,
            job_id=job_id,
            company=portal_settings.company,
            environment=environment,
        )
        return _json_response(payload)

    def on_api_spreadsheet_engine_workbook(request: Request) -> Response:
        portal_settings, _session, failure = _semantics_portal_settings(request)
        if failure is not None:
            return failure
        from urllib.parse import quote

        from hiveflow.dna.web.portal.spreadsheet_engine.service import load_catalog_workbook

        catalog_id = str(request.args.get("catalog_id") or "").strip()
        if not catalog_id:
            return _json_response({"error": "catalog_id is required"}, status=400)
        payload = load_catalog_workbook(portal_settings, catalog_id=catalog_id)
        if not payload:
            return _json_response({"error": "Workbook not found."}, status=404)
        filename = str(payload.get("filename") or "workbook.xlsx")
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        body = payload.get("body") or b""
        return Response(
            body,
            mimetype=str(payload.get("content_type") or "application/octet-stream"),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{safe_name}"; '
                    f"filename*=UTF-8''{quote(safe_name)}"
                ),
                "Cache-Control": "private, no-store",
            },
        )

    def _api_authorized(request: Request) -> Response | None:
        session, redirect = _authorized(request)
        if redirect is not None:
            return _json_response({"error": "authentication_required"}, status=401)
        return None

    def on_api_pack(request: Request) -> Response:
        if (failure := _api_authorized(request)) is not None:
            return failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        return _json_response(load_pack_from_settings(portal_settings).to_dict())

    def on_api_output(request: Request, output_id: str) -> Response:
        if (failure := _api_authorized(request)) is not None:
            return failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        limit_raw = request.args.get("limit")
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
        sort_column = str(request.args.get("sort_column") or "").strip() or None
        sort_direction = str(request.args.get("sort_direction") or "desc").strip().lower()
        try:
            return _json_response(
                fetch_output_rows(
                    portal_settings,
                    output_id,
                    limit=limit,
                    sort_column=sort_column,
                    sort_direction=sort_direction,
                )
            )
        except ValueError as exc:
            return _json_response({"error": str(exc)}, status=400)

    def on_api_reporting_pages(request: Request) -> Response:
        if (failure := _api_authorized(request)) is not None:
            return failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        return _json_response({"pages": list_reporting_pages_json(portal_settings)})

    def on_api_reporting_page(request: Request, subpath: str) -> Response:
        if (failure := _api_authorized(request)) is not None:
            return failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        path = f"/portal/{subpath.strip('/')}"
        try:
            return _json_response(fetch_page_data(portal_settings, path))
        except KeyError:
            return _json_response({"error": "page_not_found", "path": path}, status=404)

    def on_api_reporting_catalog(request: Request) -> Response:
        if (failure := _api_authorized(request)) is not None:
            return failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        return _json_response(build_reporting_binding_catalog(portal_settings))


    def on_api_manifest(request: Request) -> Response:
        if (failure := _api_authorized(request)) is not None:
            return failure
        session = session_from_request(request, company=company, environment=environment)
        assert session is not None
        client = _client_config(_portal_client_id(session))
        portal_settings = _portal_settings(settings, client, environment=environment, strict=tenant_strict)
        manifest = read_json_artifact(portal_settings, f"{portal_settings.gold_dna_prefix}/manifest.json")
        return _json_response(manifest or {})

    endpoints: dict[str, Callable[..., Response]] = {
        "portal_login": on_portal_login,
        "portal_logout": on_portal_logout,
        "portal_home": on_portal_home,
        "portal_executive": on_portal_executive,
        "portal_revenue": on_portal_revenue,
        "portal_revenue_trend": on_portal_revenue_trend,
        "portal_chart_demo": on_portal_chart_demo,
        "portal_configured_page": on_portal_configured_page,
        "portal_catalog": on_portal_catalog,
        "portal_catalog_output": on_portal_catalog_output,
        "portal_catalog_gold": on_portal_catalog_gold,
        "portal_catalog_silver": on_portal_catalog_silver,
        "portal_catalog_silver_entity": on_portal_catalog_silver_entity,
        "portal_dna": on_portal_dna,
        "portal_dna_kpi_generator": on_portal_dna_kpi_generator,
        "portal_dna_kpi_generator_status": on_portal_dna_kpi_generator_status,
        "portal_data_profile": on_portal_data_profile,
        "portal_data_profile_source_refresh": on_portal_data_profile_source_refresh,
        "portal_data_profile_entity": on_portal_data_profile_entity,
        "portal_model_mapping": on_portal_model_mapping,
        "portal_governance": on_portal_governance,
        "portal_governance_users": on_portal_governance_users,
        "portal_governance_config": on_portal_governance_config,
        "portal_governance_config_preview_exit": on_portal_governance_config_preview_exit,
        "portal_admin_config": on_portal_admin_config,
        "portal_admin_config_preview_exit": on_portal_admin_config_preview_exit,
        "portal_admin_users": on_portal_admin_users,
        "portal_source_docs_inspector": on_portal_source_docs_inspector,
        "portal_source_docs_inspector_source": on_portal_source_docs_inspector_source,
        "api_pack": on_api_pack,
        "api_manifest": on_api_manifest,
        "api_output": on_api_output,
        "api_reporting_pages": on_api_reporting_pages,
        "api_reporting_page": on_api_reporting_page,
        "api_reporting_catalog": on_api_reporting_catalog,
        "api_source_docs_gold": on_api_source_docs_gold,
        "api_source_docs_gold_build": on_api_source_docs_gold_build,
        "api_source_docs_gold_exclude": on_api_source_docs_gold_exclude,
        "api_source_docs_gold_undo_exclude": on_api_source_docs_gold_undo_exclude,
        "api_source_docs_gold_submit": on_api_source_docs_gold_submit,
        "api_source_docs_gold_versions": on_api_source_docs_gold_versions,
        "api_source_docs_gold_versions_commit": on_api_source_docs_gold_versions_commit,
        "api_source_docs_gold_restore": on_api_source_docs_gold_restore,
        "api_spreadsheet_engine_status": on_api_spreadsheet_engine_status,
        "api_spreadsheet_engine_workbook": on_api_spreadsheet_engine_workbook,
    }

    return rules, endpoints
