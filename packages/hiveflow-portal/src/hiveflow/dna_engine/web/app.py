"""DNA Engine UI — a small, self-contained FastAPI app.

Hosts every DNA-specific portal surface that used to live inside the
production portal's Werkzeug app and its shared, multi-tenant ``PortalStack``
Lambda: catalog/pack browser, governance (compile/validate/publish/restore),
data profile, model mapping, the source docs browser, and the KPI Generator.
Deliberately not part of that app — its own app, its own Lambda, its own
subdomain — sharing only the portal's session/tenant machinery (``auth.py``/
``tenant.py``) rather than being mounted in-process. See
``docs/dna-engine.md`` and ``hiveflow.spreadsheet_lab.web``, the sibling app
this one's shape is modeled on.

Unlike Spreadsheet Engine, this app freely imports ``hiveflow.dna.web``'s
Python internals (``theme``, ``portal.dna_nav``, and every ``portal.views``
render function) rather than hand-duplicating nav/theme constants — it has no
competing pyarrow/pandas bundle-size pressure (it already needs the full
``hiveflow-dna`` package), so importing is strictly less code and less drift
risk than copying.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from hiveflow.dna.web.portal.auth import (
    is_global_portal_admin,
    list_configured_portal_client_ids,
    normalize_portal_client_id,
)
from hiveflow.dna_engine.web.auth import is_portal_admin, portal_login_url, portal_session_from_request
from hiveflow.dna_engine.web.tenant import PortalTenantUnresolved, hosting_environment, tenant_scope

# Static assets borrowed directly from the real portal's theme (see
# packages/hiveflow-portal/src/hiveflow/dna/web/static/) — same whitelist
# pattern as hiveflow.spreadsheet_lab.web.app, so the two apps' look can't
# drift out of sync and stays a plain in-package file read, not a new
# dependency. DNA Engine still serves it itself (rather than redirecting to
# the shell) so hiveflow.dna.web.theme's generated /static/... hrefs resolve
# on this app's own origin too.
_DNA_STATIC_ASSETS: dict[str, str] = {
    "theme.css": "text/css",
    "hiveflowai-logo.svg": "image/svg+xml",
    "hiveflowai-logo-reversed.svg": "image/svg+xml",
    "hiveflowai-logo-mono.svg": "image/svg+xml",
}


@lru_cache(maxsize=None)
def _static_asset_version(filename: str) -> str:
    try:
        from importlib.resources import files

        data = (files("hiveflow.dna.web") / "static" / filename).read_bytes()
    except OSError:
        return "0"
    return hashlib.sha256(data).hexdigest()[:10]


def _env_config() -> dict[str, Any]:
    from hiveflow.dna.web.portal.config import load_platform_env_config

    return load_platform_env_config(hosting_environment())


def create_dna_engine_app() -> FastAPI:
    app = FastAPI(title="DNA Engine", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _session_auth(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz" or request.url.path.startswith("/static/"):
            return await call_next(request)

        session = portal_session_from_request(request)
        if session is None:
            if request.method == "GET":
                return RedirectResponse(portal_login_url(str(request.url)), status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # Stale cookie for a removed/renamed tenant — force re-auth, mirroring
        # portal.routes._authorized's tenant-strict staleness check.
        client_id = normalize_portal_client_id(session.client_id)
        known = client_id in list_configured_portal_client_ids(_env_config())
        if not known and not is_global_portal_admin(username=session.username, client_id=client_id):
            return RedirectResponse(portal_login_url(str(request.url)), status_code=303)

        try:
            with tenant_scope(client_id) as scope:
                request.state.session = session
                request.state.dna_settings = scope.settings
                request.state.client = scope.client
                request.state.is_admin = is_portal_admin(session.username)
                return await call_next(request)
        except PortalTenantUnresolved as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"ok": True, "service": "dna-engine"})

    @app.get("/static/{filename}")
    async def dna_static(filename: str) -> Response:  # pragma: no cover - static passthrough
        content_type = _DNA_STATIC_ASSETS.get(filename)
        if not content_type:
            return Response(status_code=404)
        from importlib.resources import files

        data = (files("hiveflow.dna.web") / "static" / filename).read_bytes()
        return Response(data, media_type=content_type, headers={"Cache-Control": "public, max-age=3600"})

    from hiveflow.dna_engine.web.routes import register_routes

    register_routes(app)

    return app


_app: FastAPI | None = None


def get_dna_engine_app() -> FastAPI:
    """Cached app instance for the Lambda container (reused across warm invocations)."""
    global _app  # noqa: PLW0603 — Lambda container reuse
    if _app is None:
        _app = create_dna_engine_app()
    return _app
