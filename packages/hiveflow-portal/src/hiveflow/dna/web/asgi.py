"""ASGI entrypoint for the portal — FastAPI shell over the existing WSGI app.

Incremental FastAPI migration (see the plan): pass 1 stands up a FastAPI app with
``/healthz`` and the API-Gateway stage-prefix middleware, and mounts the current
Werkzeug ``create_app`` WSGI application as the catch-all. Later passes replace
route groups (``/portal/*`` → ``/api/*`` → ``/admin/*``) with native handlers and
finally drop the WSGI mount.

Deployment is unchanged: the same three Lambdas, now via Mangum instead of
``aws-wsgi`` (see ``lambda_handler.py``). ``_prepare_gateway_environ`` in
``app.py`` still runs for the mounted WSGI app but early-exits because
``_StagePrefixMiddleware`` has already set ``scope["root_path"]``.
"""

from __future__ import annotations

import os
from typing import Any

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.app import create_app
from hiveflow.dna.web.portal.auth import clear_session_cookie
from hiveflow.dna.web.theme import BRAND_NAME


def _host_is_execute_api(headers: list[tuple[bytes, bytes]]) -> bool:
    for key, value in headers:
        if key == b"host":
            host = value.decode("latin-1").lower()
            return "execute-api" in host and "amazonaws.com" in host
    return False


class _StagePrefixMiddleware:
    """Replicate ``app._prepare_gateway_environ`` at the ASGI layer.

    Mangum leaves ``root_path`` empty; the portal needs generated links prefixed
    with the API Gateway stage on ``execute-api`` URLs but *not* on custom
    domains. When the stage is present in the path itself we also strip it.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("root_path"):
            await self.app(scope, receive, send)
            return

        stage = ""
        event = scope.get("aws.event")
        if isinstance(event, dict):
            rc = event.get("requestContext")
            if isinstance(rc, dict):
                stage = str(rc.get("stage") or "").strip()

        path = scope.get("path") or "/"
        headers = scope.get("headers") or []

        if stage:
            prefix = f"/{stage}"
            if path == prefix or path.startswith(f"{prefix}/"):
                scope = dict(scope)
                scope["root_path"] = prefix
                scope["path"] = path[len(prefix) :] or "/"
                await self.app(scope, receive, send)
                return
            if _host_is_execute_api(headers):
                scope = dict(scope)
                scope["root_path"] = prefix
                await self.app(scope, receive, send)
                return

        for stage_name in ("prod", "dev", "staging"):
            prefix = f"/{stage_name}"
            if path == prefix or path.startswith(f"{prefix}/"):
                scope = dict(scope)
                scope["root_path"] = prefix
                scope["path"] = path[len(prefix) :] or "/"
                break

        await self.app(scope, receive, send)


def create_asgi_app(
    settings: DnaSettings,
    *,
    company: str = "poc",
    environment: str = "dev",
    env_config: dict[str, Any] | None = None,
    ui_mode: str | None = None,
) -> FastAPI:
    wsgi_app = create_app(
        settings,
        company=company,
        environment=environment,
        env_config=env_config,
        ui_mode=ui_mode,
    )

    app = FastAPI(title=f"{BRAND_NAME} portal", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_StagePrefixMiddleware)
    app.state.company = company
    app.state.environment = environment

    def _prefixed(request: Request, path: str) -> str:
        return f"{request.scope.get('root_path', '')}{path}"

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"ok": True, "service": "hiveflow-portal"})

    @app.get("/portal/logout")
    @app.post("/portal/logout")
    async def portal_logout(request: Request) -> Response:
        resp = RedirectResponse(_prefixed(request, "/portal/login"), status_code=302)
        clear_session_cookie(resp)
        return resp

    # Everything not matched above falls through to the existing Werkzeug app.
    app.mount("/", WSGIMiddleware(wsgi_app))
    return app


_asgi_app: FastAPI | None = None


def get_asgi_app() -> FastAPI:
    """Cached ASGI app for the Lambda container (mirrors app._get_wsgi_app)."""
    global _asgi_app  # noqa: PLW0603 — Lambda container reuse
    if _asgi_app is None:
        from hiveflow.dna.runtime import resolve_dna_settings
        from hiveflow.project_config import (
            ensure_writable_config_path,
            get_environment_config,
            get_platform_environment_config,
            resolve_selection,
        )

        ensure_writable_config_path()
        company, environment = resolve_selection()
        try:
            env_config = get_platform_environment_config(environment)
        except KeyError:
            env_config = get_environment_config(company, environment)

        _asgi_app = create_asgi_app(
            resolve_dna_settings(),
            company=company,
            environment=environment,
            env_config=env_config,
            ui_mode=os.getenv("HIVEFLOW_UI_MODE"),
        )
    return _asgi_app
