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
from hiveflow.spreadsheet_lab.web.auth import challenge_unless_authorized
from hiveflow.spreadsheet_lab.web.templating import render_template


def create_spreadsheet_lab_app() -> FastAPI:
    app = FastAPI(title="Spreadsheet Lab", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _basic_auth(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz":
            return await call_next(request)
        challenge = challenge_unless_authorized(request)
        if challenge is not None:
            return challenge
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pragma: no cover - trivial
        return JSONResponse({"ok": True, "service": "spreadsheet-lab"})

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
        return render_template("job_detail.html", job=job, tables=tables)

    @app.post("/jobs/{job_id}/force-rerun")
    async def force_rerun(job_id: str) -> RedirectResponse:
        intake.force_rerun(job_id)
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
