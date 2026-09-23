"""HiveFlowAI web application — public site + authenticated client portal."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from werkzeug.routing import Map, Rule
from werkzeug.exceptions import NotFound
from werkzeug.wrappers import Request, Response

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.auth import (
    PortalTenantUnresolved,
    effective_portal_client_id,
    session_from_request,
)
from hiveflow.dna.web.portal.auth_login import load_portal_users
from hiveflow.dna.web.branding import load_branding_asset
from hiveflow.dna.web.admin.routes import ADMIN_UI_ENDPOINTS, build_admin_routes
from hiveflow.dna.web.routing_helpers import _app_url, _json_response
from hiveflow.dna.web.public.routes import PUBLIC_ENDPOINTS, build_public_rules
from hiveflow.dna.web.portal.routes import (
    GLOBAL_UI_ENDPOINTS,
    REPORTING_UI_ENDPOINTS,
    _client_reporting_site_url,
    build_portal_routes,
    request_tenant_scope,
)
from hiveflow.dna.web.portal.dna_nav import dna_engine_site_url
from hiveflow.dna.web.portal.tenant_credentials import TenantCredentialsError
from hiveflow.dna.web.theme import BRAND_NAME, MIME_TYPES, STATIC_DIR

# Reporting Engine legacy paths — still served in-process by this app.
LEGACY_REDIRECTS = {
    "/executive": "/portal/executive",
    "/revenue": "/portal/revenue",
    "/kpis": "/portal/executive",
}

# DNA/Governance/Agents legacy paths — that content now lives on DNA Engine's
# own subdomain (see docs/dna-engine.md), so these redirect cross-subdomain
# via dna_engine_site_url() rather than resolving in-process. Falls back to
# the bare relative path when no DNA Engine hostname is configured (local
# dev without multi-tenant DNS wiring) — same graceful degradation every
# other dna_engine_site_url() call site uses.
DNA_ENGINE_LEGACY_REDIRECTS = {
    "/definitions": "/governance",
    "/semantics": "/semantics/source-docs",
    "/portal/semantics": "/semantics/source-docs",
    "/portal/admin/users": "/governance/users",
    "/portal/admin/config": "/governance/config",
    "/portal/admin/config/preview/exit": "/governance/config/preview/exit",
}


def _api_gateway_stage(environ: dict[str, Any]) -> str:
    """Resolve API Gateway stage from awsgi event or headers."""
    event = environ.get("awsgi.event")
    if isinstance(event, dict):
        request_context = event.get("requestContext")
        if isinstance(request_context, dict):
            stage = str(request_context.get("stage", "")).strip()
            if stage:
                return stage
    return str(environ.get("HTTP_X_AMZN_APIGATEWAY_STAGE", "")).strip()


def _is_execute_api_host(environ: dict[str, Any]) -> bool:
    host = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "").lower()
    return "execute-api" in host and "amazonaws.com" in host


def _prepare_gateway_environ(environ: dict[str, Any]) -> None:
    """Normalize API Gateway stage prefix into SCRIPT_NAME for link generation."""
    if environ.get("SCRIPT_NAME"):
        return

    path_info = environ.get("PATH_INFO") or "/"
    stage = _api_gateway_stage(environ)

    if stage:
        prefix = f"/{stage}"
        if path_info == prefix or path_info.startswith(f"{prefix}/"):
            environ["SCRIPT_NAME"] = prefix
            remainder = path_info[len(prefix) :]
            environ["PATH_INFO"] = remainder or "/"
            return

        # execute-api URLs include the stage in the browser path but awsgi may
        # pass PATH_INFO without it — still prefix generated links.
        if _is_execute_api_host(environ):
            environ["SCRIPT_NAME"] = prefix
            return

    for stage_name in ("prod", "dev", "staging"):
        prefix = f"/{stage_name}"
        if path_info == prefix or path_info.startswith(f"{prefix}/"):
            environ["SCRIPT_NAME"] = prefix
            remainder = path_info[len(prefix) :]
            environ["PATH_INFO"] = remainder or "/"
            return


def _serve_static(filename: str) -> Response:
    safe_name = Path(filename).name
    body = load_branding_asset(safe_name)
    if body is None:
        asset_path = STATIC_DIR / safe_name
        if not asset_path.is_file():
            return Response("Not found", status=404)
        body = asset_path.read_bytes()

    suffix = Path(safe_name).suffix.lower()
    mime = MIME_TYPES.get(suffix) or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    dev_mode = os.getenv("HIVEFLOW_DEV", "").strip().lower() in {"1", "true", "yes"}
    cache_control = "no-cache" if dev_mode else "public, max-age=86400"
    return Response(
        body,
        mimetype=mime,
        headers={"Cache-Control": cache_control},
    )


# ``reporting_multitenant`` is ``reporting`` with the tenant resolved per request
# from the session ``client_id`` instead of a per-Lambda ``HIVEFLOW_PORTAL_CLIENT_ID``.
UI_MODES = {"full", "global", "reporting", "reporting_multitenant", "admin"}


def _resolve_ui_mode(ui_mode: str | None = None) -> str:
    resolved = (ui_mode or os.getenv("HIVEFLOW_UI_MODE", "full")).strip().lower()
    if resolved not in UI_MODES:
        return "full"
    return resolved


def create_app(
    settings: DnaSettings,
    *,
    company: str = "poc",
    environment: str = "dev",
    env_config: dict[str, Any] | None = None,
    ui_mode: str | None = None,
):
    env_config = env_config or {}
    resolved_ui_mode = _resolve_ui_mode(ui_mode)
    # In multi-tenant reporting the Lambda serves every client; the tenant comes
    # from the authenticated session, never a pinned env var.
    fixed_client_id = (
        ""
        if resolved_ui_mode == "reporting_multitenant"
        else os.getenv("HIVEFLOW_PORTAL_CLIENT_ID", "").strip().lower()
    )
    global_login_url = os.getenv("HIVEFLOW_GLOBAL_LOGIN_URL", "").strip()

    rules: list[Rule] = []
    admin_rules: list[Rule] = []
    admin_endpoints: dict[str, Any] = {}
    if resolved_ui_mode == "admin":
        admin_rules, admin_endpoints = build_admin_routes(
            company=company, environment=environment
        )
    rules.extend(admin_rules)
    if resolved_ui_mode in {"full", "global"}:
        rules.extend(build_public_rules())

    portal_rules, portal_endpoints = build_portal_routes(
        settings,
        company=company,
        environment=environment,
        env_config=env_config,
        resolved_ui_mode=resolved_ui_mode,
        fixed_client_id=fixed_client_id,
        global_login_url=global_login_url,
    )
    rules.extend(portal_rules)
    rules.append(Rule("/static/<path:filename>", endpoint="static"))

    url_map = Map(rules, strict_slashes=False)

    def on_static(_request: Request, filename: str) -> Response:
        return _serve_static(filename)

    enabled_endpoints = set(GLOBAL_UI_ENDPOINTS) | set(REPORTING_UI_ENDPOINTS)
    if resolved_ui_mode == "global":
        enabled_endpoints = GLOBAL_UI_ENDPOINTS
    elif resolved_ui_mode in {"reporting", "reporting_multitenant"}:
        enabled_endpoints = REPORTING_UI_ENDPOINTS
    elif resolved_ui_mode == "admin":
        enabled_endpoints = ADMIN_UI_ENDPOINTS

    endpoints = {
        **PUBLIC_ENDPOINTS,
        **admin_endpoints,
        **portal_endpoints,
        "static": on_static,
    }

    def application(environ, start_response):
        _prepare_gateway_environ(environ)
        request = Request(environ)
        path = request.path.rstrip("/") or "/"
        dna_engine_target = DNA_ENGINE_LEGACY_REDIRECTS.get(path)
        if dna_engine_target is not None:
            location = dna_engine_site_url(dna_engine_target)
            response = Response(status=302, headers={"Location": location})
            return response(environ, start_response)

        legacy_target = LEGACY_REDIRECTS.get(path)
        if legacy_target is not None:
            if resolved_ui_mode == "global":
                session = session_from_request(request, company=company, environment=environment)
                if session is not None:
                    reporting_url = _client_reporting_site_url(
                        effective_portal_client_id(session, fixed_client_id=fixed_client_id)
                    )
                    if reporting_url:
                        suffix = legacy_target.removeprefix("/portal")
                        location = f"{reporting_url.rstrip('/')}{suffix or ''}"
                        response = Response(status=302, headers={"Location": location})
                        return response(environ, start_response)
            location = _app_url(request, legacy_target)
            response = Response(status=302, headers={"Location": location})
            return response(environ, start_response)

        adapter = url_map.bind_to_environ(environ)
        try:
            with request_tenant_scope():
                endpoint, values = adapter.match()
                if endpoint not in enabled_endpoints:
                    response = Response("Not found", status=404)
                    return response(environ, start_response)
                response = endpoints[endpoint](request, **values)
        except NotFound:
            response = Response("Not found", status=404)
        except PortalTenantUnresolved as exc:
            response = Response(exc.message, status=403, mimetype="text/plain")
        except TenantCredentialsError as exc:
            response = Response(str(exc), status=503, mimetype="text/plain")
        except Exception as exc:  # noqa: BLE001 — surface errors in dev UI
            response = _json_response({"error": str(exc)}, status=500)
        return response(environ, start_response)

    application.load_portal_users = lambda: load_portal_users(company=company, environment=environment)  # type: ignore[attr-defined]
    return application


def run_server(
    settings: DnaSettings,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    reload: bool = False,
) -> None:
    from hiveflow.project_config import (
        get_environment_config,
        get_platform_environment_config,
        resolve_selection,
    )

    import uvicorn

    from hiveflow.dna.web.asgi import create_asgi_app

    company, environment = resolve_selection()
    try:
        env_config = get_platform_environment_config(environment)
    except KeyError:
        env_config = get_environment_config(company, environment)
    print(f"{BRAND_NAME} at http://{host}:{port}/")
    print(f"Client portal login at http://{host}:{port}/portal/login")
    if not reload and os.getenv("HIVEFLOW_DEV", "").strip().lower() in {"1", "true", "yes"}:
        reload = True
    if reload:
        # uvicorn --reload needs an import string; expose one that rebuilds the app.
        os.environ.setdefault("HIVEFLOW_SERVE_HOST", host)
        print("Dev reload enabled — code changes restart the server automatically.")
        uvicorn.run("hiveflow.dna.web.asgi:get_asgi_app", factory=True, host=host, port=port, reload=True)
        return
    app = create_asgi_app(settings, company=company, environment=environment, env_config=env_config)
    uvicorn.run(app, host=host, port=port)
