"""Spreadsheet Lab UI — a small, self-contained FastAPI app.

Deliberately not part of the production portal's Werkzeug app or its
2,700+ line ``routes.py`` — this is its own app, its own routes, its own
templates, matching the "separate, parallel process" the sandbox is for.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from hiveflow.spreadsheet_lab import clean_review, extract_review, intake, store
from hiveflow.spreadsheet_lab.web.auth import (
    SESSION_COOKIE_NAME,
    auth_required,
    is_session_valid,
    make_session_cookie_value,
    verify_login,
)
from hiveflow.spreadsheet_lab.web.templating import render_template


# Ordered left-to-right; a table's current (phase, status) maps to exactly one
# of these via `_lane_for_table` — this is the Kanban board's column set.
# "discarded" is last and reachable from any other lane (drag-and-drop and
# the per-table Discard buttons both land there), everything else only ever
# moves strictly forward.
_LANE_DEFS: list[tuple[str, str]] = [
    ("extracting", "1 · Extracting"),
    ("extract_approved", "2 · Approved — awaiting cleaning"),
    ("cleaning_shape", "3 · Cleaning: shape"),
    ("cleaning_transform", "4 · Cleaning: transform"),
    ("done", "5 · Done"),
    ("discarded", "Discarded"),
]
_LANE_ORDER: dict[str, int] = {key: index for index, (key, _label) in enumerate(_LANE_DEFS)}

# Which lanes have a bulk "approve all" action, and which single-table
# approve function it fans out to — only lanes with actionable
# (pending-review) tables get one; extract_approved/done/discarded have
# nothing to approve.
_LANE_APPROVE_ACTIONS: dict[str, tuple[str, str]] = {
    "extracting": ("pending_review", "approve_extraction"),
    "cleaning_shape": ("pending_shape_review", "approve_clean_shape"),
    "cleaning_transform": ("pending_transform_review", "approve_transformation"),
}


def _lane_for_table(table: dict[str, Any]) -> str:
    if table.get("status") == "discarded":
        return "discarded"
    phase = table.get("phase")
    status = table.get("status")
    if phase == "extract":
        return "extract_approved" if status == "approved" else "extracting"
    if phase == "clean":
        if table.get("clean_shape_status") != "approved":
            return "cleaning_shape"
        return "cleaning_transform"
    if phase == "done":
        return "done"
    return "extracting"


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


def _safe_next(path: str) -> str:
    """Only ever redirect back to a same-site relative path — a `next` value
    is user-controlled (query/form input), so guard against it being used as
    an open redirect."""
    if path.startswith("/") and not path.startswith("//"):
        return path
    return "/"


def create_spreadsheet_lab_app() -> FastAPI:
    app = FastAPI(title="Spreadsheet Lab", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _session_auth(request: Request, call_next: Any) -> Any:
        if request.url.path in ("/healthz", "/login") or not auth_required():
            return await call_next(request)
        if is_session_valid(request.cookies.get(SESSION_COOKIE_NAME)):
            return await call_next(request)
        if request.method == "GET":
            return RedirectResponse(f"/login?next={_safe_next(request.url.path)}", status_code=303)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"ok": True, "service": "spreadsheet-lab"})

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(next: str = "/") -> str:
        return render_template("login.html", next=_safe_next(next), error=None)

    @app.post("/login")
    async def login_submit(
        username: str = Form(...), password: str = Form(...), next: str = Form("/")
    ) -> Any:
        safe_next = _safe_next(next)
        if not verify_login(username, password):
            return HTMLResponse(
                render_template("login.html", next=safe_next, error="Invalid username or password."),
                status_code=401,
            )
        response = RedirectResponse(safe_next, status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            make_session_cookie_value(username),
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        jobs = store.list_jobs(limit=25)
        return render_template("index.html", jobs=jobs)

    @app.post("/upload")
    async def upload(file: UploadFile) -> RedirectResponse:
        filename = file.filename or "workbook.xlsx"
        body = await file.read()
        job = intake.create_job(filename=filename, username="")
        intake.store_upload(job["job_id"], filename=filename, body=body)
        intake.run_parse(job["job_id"])
        return RedirectResponse(f"/jobs/{job['job_id']}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(job_id: str) -> str:
        job = store.load_job(job_id)
        if not job:
            return render_template("index.html", jobs=store.list_jobs(limit=25))
        tables = store.load_tables(job_id)
        lanes = _board_lanes(tables)
        return render_template("job_detail.html", job=job, tables=tables, lanes=lanes)

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
