"""Spreadsheet Lab job/table persistence — the single source of truth per entity.

Unlike the production engine's ``hiveflow.spreadsheet.jobs`` (one 1,800+ line
module mixing storage, orchestration, and business rules), this module does
storage only: a job doc that lists table ids and overall status, and one doc
per table that is the sole authoritative record of that table's state. Nothing
about a table is ever duplicated into the job doc, so there is no "which one
is stale" question the way ``report.json`` vs ``tables/{id}.json`` has in the
production engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from hiveflow.compat import UTC
from hiveflow.storage.blobstore import (
    read_bytes,
    read_json,
    resolve_blob_location,
    write_bytes,
    write_json,
)
from hiveflow.storage.paths import (
    spreadsheet_engine_job_key,
    spreadsheet_engine_job_parse_key,
    spreadsheet_engine_job_prefix,
    spreadsheet_engine_job_table_key,
    spreadsheet_engine_job_tables_prefix,
    spreadsheet_engine_job_upload_key,
    spreadsheet_engine_jobs_list_prefix,
)

JOB_KIND = "spreadsheet_lab_job"
TABLE_KIND = "spreadsheet_lab_table"

# Job status progression. Milestone 1 only drives through the extract-review
# statuses; clean/materialize statuses exist so the schema doesn't change shape
# once those phases land.
JOB_STATUSES = (
    "uploaded",
    "parsing",
    "awaiting_extract_review",
    "extracting",
    "awaiting_clean_review",
    "cleaning",
    "materializing",
    "ready",
    "no_tables_found",
    "error",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def now_iso() -> str:
    return _now_iso()


def new_job_id() -> str:
    return uuid.uuid4().hex


# ── job ──────────────────────────────────────────────────────────────────────


def create_job(*, filename: str, username: str = "", company: str = "") -> dict[str, Any]:
    job_id = new_job_id()
    now = _now_iso()
    job = {
        "kind": JOB_KIND,
        "job_id": job_id,
        "filename": filename,
        "status": "uploaded",
        "created_at": now,
        "updated_at": now,
        "created_by": username,
        # The tenant this job's data lives under — carried into async
        # self-invoke/materialize Lambda payloads so those separate
        # invocations can rebind to the right bucket/credentials (see
        # worker._worker_tenant_scope). Empty in single-tenant/local setups.
        "company": company,
        "file_shape_hash": "",
        "matched_file_recipe_id": "",
        "auto_replayed": False,
        "table_ids": [],
        "error": "",
    }
    return save_job(job)


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    job["updated_at"] = _now_iso()
    write_json(resolve_blob_location(), spreadsheet_engine_job_key(job_id), job)
    return job


def load_job(job_id: str) -> dict[str, Any] | None:
    return read_json(resolve_blob_location(), spreadsheet_engine_job_key(job_id))


def set_job_status(job_id: str, status: str, *, error: str = "") -> dict[str, Any]:
    if status not in JOB_STATUSES:
        raise ValueError(f"Unknown job status: {status!r}")
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    job["status"] = status
    if status == "error":
        job["error"] = error
    return save_job(job)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    loc = resolve_blob_location()
    jobs: list[dict[str, Any]] = []
    if loc.bucket:
        from hiveflow.storage.aws import s3_client

        client = s3_client()
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=loc.bucket, Prefix=spreadsheet_engine_jobs_list_prefix()):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith("/job.json"):
                    keys.append(key)
        keys.sort(reverse=True)
        for key in keys[:limit]:
            payload = read_json(loc, key)
            if payload:
                jobs.append(payload)
    else:
        root = loc.data_dir
        from hiveflow.storage.paths import prefix_path

        jobs_root = prefix_path(root, spreadsheet_engine_jobs_list_prefix())
        if jobs_root.exists():
            for job_dir in sorted(jobs_root.iterdir(), reverse=True):
                job_file = job_dir / "job.json"
                if job_file.exists():
                    payload = read_json(loc, spreadsheet_engine_job_key(job_dir.name))
                    if payload:
                        jobs.append(payload)
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return jobs[:limit]


# ── upload ───────────────────────────────────────────────────────────────────


def store_upload(job_id: str, *, filename: str, body: bytes) -> str:
    key = spreadsheet_engine_job_upload_key(job_id, filename)
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.lower().endswith((".xlsx", ".xlsm"))
        else "application/octet-stream"
    )
    location = write_bytes(resolve_blob_location(), key, body, content_type=content_type)
    job = load_job(job_id) or {}
    job["upload_key"] = key
    job["filename"] = filename
    save_job(job)
    return location


def load_upload_bytes(job: dict[str, Any]) -> bytes:
    job_id = str(job.get("job_id") or "")
    filename = str(job.get("filename") or "workbook.xlsx")
    upload_key = str(job.get("upload_key") or spreadsheet_engine_job_upload_key(job_id, filename))
    return read_bytes(resolve_blob_location(), upload_key)


# ── parse cache ──────────────────────────────────────────────────────────────


def save_parse(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    write_json(resolve_blob_location(), spreadsheet_engine_job_parse_key(job_id), payload)
    return payload


def load_parse(job_id: str) -> dict[str, Any] | None:
    return read_json(resolve_blob_location(), spreadsheet_engine_job_parse_key(job_id))


# ── tables ───────────────────────────────────────────────────────────────────


def new_table(
    *,
    job_id: str,
    table_id: str,
    sheet: str,
    source_region: dict[str, Any],
    input_shape: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": TABLE_KIND,
        "job_id": job_id,
        "table_id": table_id,
        "sheet": sheet,
        "phase": "extract",
        "status": "pending_review",
        "attempt_count": 0,
        "feedback_history": [],
        "source_region": source_region,
        "extract_proposal": None,
        "input_shape": input_shape,
        "clean_goal": None,
        "transformation": {"version": 1, "steps": []},
        "transformation_status": "awaiting_shape",
        "silver": None,
    }


def save_table(table: dict[str, Any]) -> dict[str, Any]:
    job_id = str(table.get("job_id") or "").strip()
    table_id = str(table.get("table_id") or "").strip()
    if not job_id or not table_id:
        raise ValueError("job_id and table_id are required")
    write_json(resolve_blob_location(), spreadsheet_engine_job_table_key(job_id, table_id), table)
    return table


def load_table(job_id: str, table_id: str) -> dict[str, Any] | None:
    return read_json(resolve_blob_location(), spreadsheet_engine_job_table_key(job_id, table_id))


def list_table_ids(job_id: str) -> list[str]:
    loc = resolve_blob_location()
    prefix = spreadsheet_engine_job_tables_prefix(job_id)
    if loc.bucket:
        from hiveflow.storage.blobstore import list_keys

        keys = list_keys(loc, prefix, suffix=".json")
        return sorted({key.rsplit("/", 1)[-1][:-5] for key in keys})
    from hiveflow.storage.paths import prefix_path

    root = prefix_path(loc.data_dir, prefix)
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.json"))


def load_tables(job_id: str) -> list[dict[str, Any]]:
    job = load_job(job_id) or {}
    table_ids = [str(tid) for tid in (job.get("table_ids") or []) if str(tid).strip()]
    if not table_ids:
        table_ids = list_table_ids(job_id)
    tables: list[dict[str, Any]] = []
    for table_id in table_ids:
        table = load_table(job_id, table_id)
        if table:
            tables.append(table)
    return tables


def promote_extract_proposal(table: dict[str, Any]) -> None:
    """Copy entity_name/purpose/grain/schema from extract_proposal onto the
    table doc itself.

    ``hiveflow.spreadsheet.transform.build_output_shape`` and downstream
    materialization read these directly off the table dict (matching the
    production engine's table shape) rather than reaching into
    ``extract_proposal`` — shared by the live approve flow
    (``extract_review.approve_extraction``) and file-recipe replay
    (``file_recipe.replay_file_recipe``), so both leave the table doc in the
    same shape.
    """
    proposal = table.get("extract_proposal") or {}
    table["entity_name"] = str(proposal.get("entity_name") or table.get("table_id") or "")
    table["purpose"] = str(proposal.get("purpose") or "")
    table["grain"] = str(proposal.get("grain") or "")
    table["schema"] = list(proposal.get("schema") or [])


def append_feedback(table: dict[str, Any], *, text: str, by: str = "") -> dict[str, Any]:
    history = list(table.get("feedback_history") or [])
    history.append({"text": text, "at": _now_iso(), "by": by})
    table["feedback_history"] = history
    table["attempt_count"] = int(table.get("attempt_count") or 0) + 1
    return table


def job_prefix_for(job_id: str) -> str:
    return spreadsheet_engine_job_prefix(job_id)
