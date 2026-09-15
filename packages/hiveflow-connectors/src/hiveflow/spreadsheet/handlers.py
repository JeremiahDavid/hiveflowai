"""AWS Lambda handlers for Spreadsheet Engine Step Functions.

These are global/shared across every company (see ``infra/spreadsheet_pipeline.py``):
one deployment, invoked with ``company`` in the event rather than one deployment
per company. Each handler assumes that company's tenant role for the duration
of the call (``hiveflow.tenant_credentials``) and points ``hiveflow.spreadsheet.jobs``
at that company's bucket before doing any S3 work — the same per-invocation
config discipline the multi-tenant portal Lambda already uses
(``hiveflow.dna.web.portal.spreadsheet_engine.service._configure_jobs_env``).
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Iterator

from hiveflow.spreadsheet.jobs import (
    run_interpret,
    run_parse,
    run_profile,
    run_propose_finalize,
    run_propose_prepare,
    run_propose_table,
)


@contextlib.contextmanager
def _tenant_scope(company: str) -> Iterator[str]:
    """Assume ``company``'s tenant role and point jobs.py at its bucket."""
    from hiveflow.project_config import resolve_data_bucket_name
    from hiveflow.tenant_credentials import tenant_credentials

    environment = os.environ.get("HIVEFLOW_ENVIRONMENT", "").strip()
    with tenant_credentials(company, environment):
        bucket = resolve_data_bucket_name(company, environment)
        previous = os.environ.get("HIVEFLOW_S3_BUCKET")
        os.environ["HIVEFLOW_S3_BUCKET"] = bucket
        try:
            yield company
        finally:
            if previous is None:
                os.environ.pop("HIVEFLOW_S3_BUCKET", None)
            else:
                os.environ["HIVEFLOW_S3_BUCKET"] = previous


def _require_company(body: dict[str, Any]) -> str:
    company = str(body.get("company") or "").strip()
    if not company:
        raise ValueError("company is required")
    return company


def parse_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    company = _require_company(body)
    with _tenant_scope(company):
        job = run_parse(job_id)
    return {"status": "ok", "job_id": job_id, "company": company, "job": job}


def profile_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    company = _require_company(body)
    with _tenant_scope(company):
        job = run_profile(job_id)
    return {"status": "ok", "job_id": job_id, "company": company, "job": job}


def interpret_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    company = _require_company(body)
    with _tenant_scope(company):
        job = run_interpret(job_id)
    return {"status": "ok", "job_id": job_id, "company": company, "job": job}


def propose_prepare_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Flip the job to 'proposing' and return table_ids for the Map state to fan out over."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    company = _require_company(body)
    with _tenant_scope(company):
        result = run_propose_prepare(job_id)
    result["company"] = company
    return result


def propose_table_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Propose a cleaned transformation for exactly one table (one Map branch)."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    table_id = str(body.get("table_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    if not table_id:
        raise ValueError("table_id is required")
    company = _require_company(body)
    with _tenant_scope(company):
        return run_propose_table(job_id, table_id)


def propose_finalize_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Aggregate the Map state's per-table proposals into the final report."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    company = _require_company(body)
    with _tenant_scope(company):
        job = run_propose_finalize(job_id)
    return {"status": "ok", "job_id": job_id, "company": company, "job": job}


def pipeline_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Local/dev convenience handler that runs the full pipeline synchronously."""
    body = event or {}
    job_id = str(body.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    from hiveflow.spreadsheet.jobs import run_pipeline

    job = run_pipeline(job_id)
    return {"status": job.get("status", "ok"), "job_id": job_id, "job": job}
