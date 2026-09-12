"""AWS Lambda handlers for Spreadsheet Engine Step Functions."""

from __future__ import annotations

from typing import Any

from hiveflow.spreadsheet.jobs import (
    run_interpret,
    run_parse,
    run_profile,
    run_propose_finalize,
    run_propose_prepare,
    run_propose_table,
)


def parse_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    job = run_parse(job_id)
    return {"status": "ok", "job_id": job_id, "job": job}


def profile_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    job = run_profile(job_id)
    return {"status": "ok", "job_id": job_id, "job": job}


def interpret_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    job = run_interpret(job_id)
    return {"status": "ok", "job_id": job_id, "job": job}


def propose_prepare_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Flip the job to 'proposing' and return table_ids for the Map state to fan out over."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    return run_propose_prepare(job_id)


def propose_table_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Propose a cleaned transformation for exactly one table (one Map branch)."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    table_id = str(body.get("table_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if not table_id:
        raise ValueError("table_id is required")
    return run_propose_table(job_id, table_id)


def propose_finalize_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Aggregate the Map state's per-table proposals into the final report."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    job = run_propose_finalize(job_id)
    return {"status": "ok", "job_id": job_id, "job": job}


def pipeline_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Local/dev convenience handler that runs the full pipeline synchronously."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    from hiveflow.spreadsheet.jobs import run_pipeline

    job = run_pipeline(job_id)
    return {"status": job.get("status", "ok"), "job_id": job_id, "job": job}
