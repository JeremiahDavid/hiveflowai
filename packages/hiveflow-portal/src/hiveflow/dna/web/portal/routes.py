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
from urllib.parse import urlencode, urlparse

from werkzeug.routing import Rule
from werkzeug.wrappers import Request, Response

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import load_pack_from_settings, read_json_artifact
from hiveflow.dna.web.portal.auth import (
    clear_session_cookie,
    client_id_from_reporting_hostname,
    effective_portal_client_id,
    is_global_portal_admin,
    is_global_portal_client_id,
    list_configured_portal_client_ids,
    normalize_portal_client_id,
    PortalTenantUnresolved,
    require_portal_admin,
    require_portal_session,
    session_from_request,
)
from hiveflow.dna.web.portal.auth_login import (
    authenticate,
    authorize_portal_client_access,
    login_response,
    PortalClientAccessError,
    resolve_login_client_id_hint,
)
from hiveflow.dna.web.portal.config import load_client_portal_config
from hiveflow.dna.web.portal.preview import preview_proposal_id
from hiveflow.dna.web.portal.reporting_layout import find_reporting_page
from hiveflow.dna.web.portal.reporting_api import (
    fetch_output_rows,
    fetch_page_data,
    list_reporting_pages_json,
)
from hiveflow.dna.web.portal.governance_helpers.gold_bindings import build_reporting_binding_catalog
from hiveflow.dna.web.portal.views import render_configured_page
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
        "static",
    }
)

# DNA/Agents/Governance/Source Browser (catalog, governance, data profile,
# model mapping, source docs, KPI Generator) moved to DNA Engine's own
# subdomain/app — see hiveflow.dna_engine.web and docs/dna-engine.md. This
# app now only serves the Reporting Engine (client dashboards) plus login.
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
        "static",
        "api_pack",
        "api_manifest",
        "api_output",
        "api_reporting_pages",
        "api_reporting_page",
        "api_reporting_catalog",
    }
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
        "api_pack": on_api_pack,
        "api_manifest": on_api_manifest,
        "api_output": on_api_output,
        "api_reporting_pages": on_api_reporting_pages,
        "api_reporting_page": on_api_reporting_page,
        "api_reporting_catalog": on_api_reporting_catalog,
    }

    return rules, endpoints
