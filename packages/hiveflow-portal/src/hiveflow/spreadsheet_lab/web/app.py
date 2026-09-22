"""Spreadsheet Engine UI — a small, self-contained FastAPI app.

Deliberately not part of the production portal's Werkzeug app or its
2,700+ line ``routes.py`` — its own app, its own routes, its own templates,
sharing only the portal's session/tenant machinery (see ``auth.py``/
``tenant.py``) rather than being mounted in-process.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from markupsafe import Markup

from hiveflow.spreadsheet_lab import clean_review, extract_review, intake, store
from hiveflow.spreadsheet_lab.web.auth import portal_login_url, portal_session_from_request
from hiveflow.spreadsheet_lab.web.templating import render_template
from hiveflow.spreadsheet_lab.web.tenant import TenantUnresolved, tenant_scope

# Static assets borrowed directly from the real portal's theme (see
# packages/hiveflow-portal/src/hiveflow/dna/web/static/) so this app's look
# matches it exactly and can't drift out of sync — both packages already live
# in hiveflow-portal, so this is a plain file read, not a new dependency.
# Whitelisted by filename to keep this a fixed, reviewable set rather than an
# arbitrary passthrough into another package's directory.
_DNA_STATIC_ASSETS: dict[str, str] = {
    "theme.css": "text/css",
    "hiveflowai-logo.svg": "image/svg+xml",
    "hiveflowai-logo-reversed.svg": "image/svg+xml",
    "hiveflowai-logo-mono.svg": "image/svg+xml",
}


@lru_cache(maxsize=None)
def _static_asset_version(filename: str) -> str:
    """Content-hash cache-buster, mirroring hiveflow.dna.web.theme's own
    ``_static_asset_version`` (not imported — see this module's docstring on
    why this app never imports dna.web internals). Without a ``?v=`` on the
    URL, an edit to theme.css sits invisible behind the browser's cache of
    ``dna_static``'s ``Cache-Control: max-age=3600`` below until it expires."""
    try:
        from importlib.resources import files

        data = (files("hiveflow.dna.web") / "static" / filename).read_bytes()
    except OSError:
        return "0"
    return hashlib.sha256(data).hexdigest()[:10]


# Ordered left-to-right; a table's current (phase, status) maps to exactly one
# of these via `_lane_for_table` — this is the Kanban board's column set.
# "discarded" is last, rendered as its own always-visible row rather than a
# board column, and reachable from any other lane (drag-and-drop and the
# per-table Discard buttons both land there); everything else only ever
# moves strictly forward. A table sitting "approved, awaiting its siblings to
# finish extraction" has no decision to make (it's not in
# _LANE_APPROVE_ACTIONS below) — it stays in "Extract" rather than getting
# its own column, since that intermediate state is pure pass-through.
_LANE_DEFS: list[tuple[str, str]] = [
    ("extracting", "1 · Extract"),
    ("cleaning_shape", "2 · Clean: shape"),
    ("cleaning_transform", "3 · Clean: transform"),
    ("done", "4 · Done"),
    ("discarded", "Discarded"),
]
_LANE_ORDER: dict[str, int] = {key: index for index, (key, _label) in enumerate(_LANE_DEFS)}

# Which lanes have a bulk "approve all" action, and which single-table
# approve function it fans out to — only lanes with actionable
# (pending-review) tables get one; done/discarded have nothing to approve.
_LANE_APPROVE_ACTIONS: dict[str, tuple[str, str]] = {
    "extracting": ("pending_review", "approve_extraction"),
    "cleaning_shape": ("pending_shape_review", "approve_clean_shape"),
    "cleaning_transform": ("pending_transform_review", "approve_transformation"),
}


def _lane_for_table(table: dict[str, Any]) -> str:
    if table.get("status") == "discarded":
        return "discarded"
    phase = table.get("phase")
    if phase == "extract":
        return "extracting"
    if phase == "clean":
        if table.get("clean_shape_status") != "approved":
            return "cleaning_shape"
        return "cleaning_transform"
    if phase == "done":
        return "done"
    return "extracting"


# Mirrors hiveflow.dna.web.portal.dna_nav.agents_section_nav() — the Agents
# sidebar every other Agents-section portal page (i.e. the DNA Engine) falls
# back to. Duplicated as plain (path, label, icon) literals rather than
# imported: dna_nav.py drags in hiveflow.dna.web.portal.catalog, which
# imports hiveflow.dna.{field_semantics,schema,store,workflow} — the full DNA
# layer's dependency footprint this Lambda's requirements file
# (requirements-lambda-spreadsheet-lab.txt) deliberately excludes (see its
# header comment). A template file has no import-time side effects, so
# _macros.html was safe to copy verbatim; this Python-level nav data isn't,
# so keep it in sync with dna_nav.py/theme.py by hand instead.
_AGENTS_NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("/portal/dna/kpi-generator", "DNA Engine", "dna"),
    ("__self__", "Spreadsheet Engine", "spreadsheet"),
)

_TOP_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("/portal", "Reporting"),
    ("/portal/dna", "DNA"),
    ("/portal/dna/kpi-generator", "Agents"),
    ("/portal/governance", "Governance"),
)

# Mirrors hiveflow.dna.web.theme._SIDE_NAV_GLYPHS (see the module-level
# comment above on why this is copied rather than imported).
_SIDE_NAV_GLYPHS: dict[str, str] = {
    "dna": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round" aria-hidden="true">'
        '<path d="M7 3c0 4 10 4 10 8s-10 4-10 8"/>'
        '<path d="M17 3c0 4-10 4-10 8s10 4 10 8"/>'
        '<path d="M8 6.5h8M8 17.5h8M7.3 12h9.4"/>'
        "</svg>"
    ),
    "spreadsheet": (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.6" stroke-linejoin="round" aria-hidden="true">'
        '<rect x="3.5" y="4" width="17" height="16" rx="1.5"/>'
        '<path d="M3.5 9.5h17M3.5 15h17M9.5 4v16M15 4v16" stroke-width="1.3"/>'
        "</svg>"
    ),
}


def _nav_abbrev(label: str) -> str:
    """Mirrors hiveflow.dna.web.theme._nav_abbrev exactly (see the module-
    level comment above on why this is copied rather than imported)."""
    words = [part for part in label.split() if part]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return label.strip()[:2].upper() or "•"


def _layout_ctx(request: Request) -> dict[str, Any]:
    """Chrome context every full-page template needs: the signed-in
    username, the Agents sidebar (this app's own nav item marked active so
    the DNA Engine stays one click away), and the top nav/logout links back
    to the real portal (this app owns no session of its own to log out of —
    see auth.py).

    DNA/reporting pages live only on the tenant's own subdomain
    (``{client_id}.{cookie_domain}``) — the bare primary site
    (``HIVEFLOW_PRIMARY_SITE_URL``) runs the portal Lambda in "global" mode,
    which serves login/marketing only and 404s on every DNA route (confirmed
    by a real repro: /portal/semantics/source-docs on the primary hostname).
    Mirrors hiveflow.dna.web.portal.routes._client_reporting_site_url.
    """
    import os

    session = portal_session_from_request(request)
    primary_site = os.getenv("HIVEFLOW_PRIMARY_SITE_URL", "").strip().rstrip("/")
    cookie_domain = os.getenv("HIVEFLOW_PORTAL_COOKIE_DOMAIN", "").strip()
    bare_cookie_domain = cookie_domain.lstrip(".")
    self_url = f"https://spreadsheet-engine{cookie_domain}/" if cookie_domain else "/"
    client_id = (session.client_id if session else "").strip().lower()
    tenant_site = f"https://{client_id}.{bare_cookie_domain}" if client_id and bare_cookie_domain else ""

    def portal_url(path: str) -> str:
        base = tenant_site or primary_site
        return f"{base}{path}" if base else path

    agents_nav_items = [
        {
            "href": self_url if href == "__self__" else portal_url(href),
            "label": label,
            "abbrev": _nav_abbrev(label),
            "icon_svg": Markup(_SIDE_NAV_GLYPHS[icon]) if icon in _SIDE_NAV_GLYPHS else None,
            "active": href == "__self__",
            "is_ancestor": False,
            "open": href == "__self__",
            "children": [],
        }
        for href, label, icon in _AGENTS_NAV_ITEMS
    ]
    top_nav_items = [
        {"href": portal_url(href), "label": label, "active": label == "Agents"}
        for href, label in _TOP_NAV_ITEMS
    ]
    return {
        "username": session.username if session else "",
        "brand_href": primary_site + "/" if primary_site else "/",
        "logout_url": portal_url("/portal/logout"),
        "agents_nav_items": agents_nav_items,
        "top_nav_items": top_nav_items,
        "theme_css_href": f"/static/theme.css?v={_static_asset_version('theme.css')}",
        "logo_href": f"/static/hiveflowai-logo.svg?v={_static_asset_version('hiveflowai-logo.svg')}",
        "logo_reversed_href": (
            f"/static/hiveflowai-logo-reversed.svg?v={_static_asset_version('hiveflowai-logo-reversed.svg')}"
        ),
        "favicon_href": f"/static/hiveflowai-logo-mono.svg?v={_static_asset_version('hiveflowai-logo-mono.svg')}",
    }


def _board_lanes(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group tables into Kanban lanes for the job-detail board."""
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _label in _LANE_DEFS}
    for table in tables:
        grouped[_lane_for_table(table)].append(table)
    return [
        {
            "key": key,
            "label": label,
            "order": _LANE_ORDER[key],
            "tables": grouped[key],
            "approve_status": _LANE_APPROVE_ACTIONS.get(key, (None, None))[0],
        }
        for key, label in _LANE_DEFS
    ]


def create_spreadsheet_lab_app() -> FastAPI:
    app = FastAPI(title="Spreadsheet Engine", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _session_auth(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz" or request.url.path.startswith("/static/"):
            return await call_next(request)
        session = portal_session_from_request(request)
        if session is None:
            if request.method == "GET":
                return RedirectResponse(portal_login_url(str(request.url)), status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            with tenant_scope(session.client_id) as company:
                request.state.company = company
                return await call_next(request)
        except TenantUnresolved as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"ok": True, "service": "spreadsheet-engine"})

    @app.get("/static/{filename}")
    async def dna_static(filename: str) -> Response:  # pragma: no cover - static passthrough
        content_type = _DNA_STATIC_ASSETS.get(filename)
        if not content_type:
            return Response(status_code=404)
        from importlib.resources import files

        data = (files("hiveflow.dna.web") / "static" / filename).read_bytes()
        return Response(data, media_type=content_type, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> str:
        jobs = store.list_jobs(limit=25)
        return render_template("index.html", jobs=jobs, **_layout_ctx(request))

    @app.post("/upload")
    async def upload(request: Request, file: UploadFile) -> RedirectResponse:
        filename = file.filename or "workbook.xlsx"
        body = await file.read()
        session = portal_session_from_request(request)
        username = session.username if session else ""
        company = str(getattr(request.state, "company", "") or "")
        job = intake.create_job(filename=filename, username=username, company=company)
        intake.store_upload(job["job_id"], filename=filename, body=body)
        intake.run_parse(job["job_id"])
        return RedirectResponse(f"/jobs/{job['job_id']}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: str) -> str:
        job = store.load_job(job_id)
        if not job:
            return render_template("index.html", jobs=store.list_jobs(limit=25), **_layout_ctx(request))
        tables = store.load_tables(job_id)
        lanes = _board_lanes(tables)
        return render_template(
            "job_detail.html", job=job, tables=tables, lanes=lanes, **_layout_ctx(request)
        )

    @app.post("/jobs/{job_id}/force-rerun")
    async def force_rerun(job_id: str) -> RedirectResponse:
        intake.force_rerun(job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/approve-all/{lane_key}")
    async def approve_all(job_id: str, lane_key: str) -> RedirectResponse:
        """Fan a lane's bulk-approve button out to the same single-table
        approve function each card's own button would call — one lane at a
        time, in table order, skipping anything that isn't actually in that
        lane's approvable status (e.g. a card mid-agent-call)."""
        approve_status, action = _LANE_APPROVE_ACTIONS.get(lane_key, (None, None))
        if action:
            for table in store.load_tables(job_id):
                if _lane_for_table(table) != lane_key or table.get("status") != approve_status:
                    continue
                table_id = str(table.get("table_id") or "")
                try:
                    if action == "approve_extraction":
                        extract_review.approve_extraction(job_id, table_id)
                    elif action == "approve_clean_shape":
                        clean_review.approve_clean_shape(job_id, table_id)
                    elif action == "approve_transformation":
                        clean_review.approve_transformation(job_id, table_id)
                except ValueError:
                    continue
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/approve")
    async def approve_table(job_id: str, table_id: str) -> RedirectResponse:
        extract_review.approve_extraction(job_id, table_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/discard")
    async def discard_table(job_id: str, table_id: str) -> RedirectResponse:
        extract_review.discard_table(job_id, table_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/reject")
    async def reject_table(job_id: str, table_id: str, feedback: str = Form(...)) -> RedirectResponse:
        extract_review.reject_extraction(job_id, table_id, feedback=feedback, by="")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/approve-clean-shape")
    async def approve_clean_shape(job_id: str, table_id: str) -> RedirectResponse:
        clean_review.approve_clean_shape(job_id, table_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/reject-clean-shape")
    async def reject_clean_shape(job_id: str, table_id: str, feedback: str = Form(...)) -> RedirectResponse:
        clean_review.reject_clean_shape(job_id, table_id, feedback=feedback, by="")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/approve-transformation")
    async def approve_transformation(job_id: str, table_id: str) -> RedirectResponse:
        clean_review.approve_transformation(job_id, table_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/reject-transformation")
    async def reject_transformation(job_id: str, table_id: str, feedback: str = Form(...)) -> RedirectResponse:
        clean_review.reject_transformation(job_id, table_id, feedback=feedback, by="")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/tables/{table_id}/discard-clean")
    async def discard_clean_table(job_id: str, table_id: str) -> RedirectResponse:
        clean_review.discard_table(job_id, table_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/api/status")
    async def api_status(job_id: str) -> JSONResponse:
        job = store.load_job(job_id)
        if not job:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        tables = store.load_tables(job_id)
        return JSONResponse(
            {
                "job": job,
                "tables": [
                    {
                        "table_id": t.get("table_id"),
                        "phase": t.get("phase"),
                        "status": t.get("status"),
                        "attempt_count": t.get("attempt_count"),
                    }
                    for t in tables
                ],
            }
        )

    return app


_app: FastAPI | None = None


def get_spreadsheet_lab_app() -> FastAPI:
    """Cached app instance for the Lambda container (reused across warm invocations)."""
    global _app  # noqa: PLW0603 — Lambda container reuse
    if _app is None:
        _app = create_spreadsheet_lab_app()
    return _app
