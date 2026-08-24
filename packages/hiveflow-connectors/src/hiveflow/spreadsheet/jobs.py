"""Spreadsheet Engine job persistence and orchestration."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from hiveflow.compat import UTC
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet.interpret import interpret_tables
from hiveflow.spreadsheet.profiler import profile_tables
from hiveflow.spreadsheet.propose import propose_transforms
from hiveflow.spreadsheet.transform import (
    build_output_shape,
    compute_input_shape,
    preview_transformation,
    slugify_filename,
)
from hiveflow.storage.paths import (
    prefix_path,
    spreadsheet_engine_catalog_entry_key,
    spreadsheet_engine_catalog_prefix,
    spreadsheet_engine_job_key,
    spreadsheet_engine_job_parse_key,
    spreadsheet_engine_job_prefix,
    spreadsheet_engine_jobs_prefix,
    spreadsheet_engine_job_profile_key,
    spreadsheet_engine_job_report_key,
    spreadsheet_engine_job_table_key,
    spreadsheet_engine_job_upload_key,
    spreadsheet_engine_knowledge_entry_key,
    spreadsheet_engine_knowledge_prefix,
)

JOB_KIND = "spreadsheet_engine_job"
CATALOG_ENTRY_KIND = "spreadsheet_engine_catalog_entry"
KNOWLEDGE_ENTRY_KIND = "spreadsheet_engine_knowledge"
TERMINAL_STATUSES = frozenset({"ready", "error"})
UPLOAD_HISTORY_LIMIT = 20


def new_job_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _bucket() -> str:
    return os.getenv("HIVEFLOW_S3_BUCKET", "").strip()


def _data_dir() -> Path:
    return Path(os.getenv("HIVEFLOW_DATA_DIR", "data")).resolve()


def _write_json(key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    bucket = _bucket()
    if bucket:
        import boto3

        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        return f"s3://{bucket}/{key}"
    path = prefix_path(_data_dir(), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


def _read_json(key: str) -> dict[str, Any] | None:
    bucket = _bucket()
    if bucket:
        import boto3

        client = boto3.client("s3")
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "NoSuchKey":
                return None
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    path = prefix_path(_data_dir(), key)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _write_bytes(key: str, body: bytes, *, content_type: str) -> str:
    bucket = _bucket()
    if bucket:
        import boto3

        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
        return f"s3://{bucket}/{key}"
    path = prefix_path(_data_dir(), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


def _read_bytes(key: str) -> bytes:
    bucket = _bucket()
    if bucket:
        import boto3

        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    path = prefix_path(_data_dir(), key)
    return path.read_bytes()


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    job["updated_at"] = _now_iso()
    _write_json(spreadsheet_engine_job_key(job_id), job)
    return job


def load_job(job_id: str) -> dict[str, Any] | None:
    return _read_json(spreadsheet_engine_job_key(job_id))


def create_job(
    *,
    filename: str,
    username: str = "",
    linked_catalog_id: str = "",
) -> dict[str, Any]:
    job_id = new_job_id()
    now = _now_iso()
    linked = str(linked_catalog_id or "").strip().lower()
    job = {
        "kind": JOB_KIND,
        "job_id": job_id,
        "status": "uploaded",
        "filename": filename,
        "created_at": now,
        "updated_at": now,
        "created_by": username,
        "chat_history": [],
        "table_ids": [],
        "execution_arn": "",
        "error": "",
        "linked_catalog_id": linked,
        "suggested_catalog_ids": [],
        "reupload": bool(linked),
    }
    return save_job(job)


def store_upload(job_id: str, *, filename: str, body: bytes) -> str:
    key = spreadsheet_engine_job_upload_key(job_id, filename)
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.lower().endswith(".xlsx")
        else "application/octet-stream"
    )
    location = _write_bytes(key, body, content_type=content_type)
    job = load_job(job_id) or {}
    job["upload_key"] = key
    job["filename"] = filename
    job["status"] = "uploaded"
    save_job(job)
    return location


def _local_upload_path(job_id: str, filename: str) -> Path:
    key = spreadsheet_engine_job_upload_key(job_id, filename)
    return prefix_path(_data_dir(), key)


def run_parse(job_id: str) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    filename = str(job.get("filename") or "workbook.xlsx")
    upload_key = str(job.get("upload_key") or spreadsheet_engine_job_upload_key(job_id, filename))
    job["status"] = "parsing"
    save_job(job)

    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(_read_bytes(upload_key))
        from hiveflow.spreadsheet.parser import parse_workbook

        selected = [str(name).strip() for name in (job.get("selected_sheets") or []) if str(name).strip()]
        parse_payload = parse_workbook(
            local_path,
            filename=filename,
            sheet_names=selected or None,
        )
    parse_payload["job_id"] = job_id
    _write_json(spreadsheet_engine_job_parse_key(job_id), parse_payload)

    job = load_job(job_id) or job
    job["parse_key"] = spreadsheet_engine_job_parse_key(job_id)
    job["sheet_names"] = list(parse_payload.get("sheet_names") or [])
    job["sheets"] = list(parse_payload.get("sheets") or [])
    job["table_ids"] = [str(t.get("table_id")) for t in parse_payload.get("tables") or []]
    selected = [str(name).strip() for name in (job.get("selected_sheets") or []) if str(name).strip()]
    auto_select = bool(job.get("reupload") or job.get("linked_catalog_id"))
    if selected:
        job["selected_sheets"] = selected
        job["status"] = "parsed"
    elif auto_select:
        job["selected_sheets"] = list(job.get("sheet_names") or [])
        job["status"] = "parsed"
    else:
        job["status"] = "awaiting_sheets"
    if not str(job.get("linked_catalog_id") or "").strip():
        suggestions = find_catalog_matches_for_parse(parse_payload)
        job["suggested_catalog_ids"] = suggestions
    save_job(job)
    return job


def run_profile(job_id: str) -> dict[str, Any]:
    job = load_job(job_id) or {}
    job["status"] = "profiling"
    save_job(job)
    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id))
    if not parse_payload:
        raise ValueError(f"Missing parse output for job {job_id!r}")
    profile_payload = profile_tables(parse_payload)
    profile_payload["job_id"] = job_id
    _write_json(spreadsheet_engine_job_profile_key(job_id), profile_payload)

    job = load_job(job_id) or job
    job["status"] = "profiled"
    job["profile_key"] = spreadsheet_engine_job_profile_key(job_id)
    return save_job(job)


def _is_reload_job(job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    if not bool(job.get("reupload")):
        return False
    return bool(str(job.get("linked_catalog_id") or "").strip())


def run_reload_prepare(job_id: str) -> dict[str, Any]:
    """Build report from catalog for a linked re-upload — no AI."""
    job = load_job(job_id) or {}
    job["status"] = "interpreting"
    save_job(job)

    linked_id = str(job.get("linked_catalog_id") or "").strip().lower()
    catalog_entry = load_catalog_entry(linked_id)
    if not catalog_entry:
        raise ValueError(f"Unknown linked catalog entry {linked_id!r}")

    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id))
    profile_payload = _read_json(spreadsheet_engine_job_profile_key(job_id))
    if not parse_payload or not profile_payload:
        raise ValueError(f"Missing parse/profile output for reload job {job_id!r}")

    parse_tables = {
        str(t.get("table_id")): t
        for t in (parse_payload.get("tables") or [])
        if isinstance(t, dict) and t.get("table_id")
    }
    profile_tables = {
        str(t.get("table_id")): t
        for t in (profile_payload.get("tables") or [])
        if isinstance(t, dict) and t.get("table_id")
    }

    if not parse_tables:
        raise ValueError(f"No tables detected in workbook for reload job {job_id!r}")

    catalog_table_id = str(catalog_entry.get("table_id") or "t0")
    table_id = catalog_table_id if catalog_table_id in parse_tables else next(iter(parse_tables), "t0")
    parse_table = parse_tables.get(table_id) or next(iter(parse_tables.values()), {})
    profile_table = profile_tables.get(table_id)

    from hiveflow.spreadsheet.reload import build_reload_table_proposal, validate_reload_table

    headers = [str(h) for h in (parse_table.get("headers") or []) if str(h).strip()]
    preview = load_table_preview(job_id, table_id, max_rows=50) or {}
    sample_rows = list(preview.get("rows") or [])
    sample_headers = list(preview.get("headers") or headers)

    validation = validate_reload_table(
        parse_table=parse_table,
        profile_table=profile_table,
        catalog_entry=catalog_entry,
        sample_headers=sample_headers,
        sample_rows=sample_rows,
    )
    table = build_reload_table_proposal(
        table_id=table_id,
        parse_table=parse_table,
        profile_table=profile_table,
        catalog_entry=catalog_entry,
        validation=validation,
    )

    report = {
        "kind": "spreadsheet_engine_report",
        "job_id": job_id,
        "filename": parse_payload.get("filename") or job.get("filename"),
        "table_count": 1,
        "tables": [table],
        "reload_mode": True,
    }
    _write_json(spreadsheet_engine_job_report_key(job_id), report)
    _write_json(spreadsheet_engine_job_table_key(job_id, table_id), table)

    job = load_job(job_id) or job
    job["status"] = "interpreted"
    job["reload_mode"] = True
    job["reload_validation_status"] = validation.get("reload_validation_status")
    job["report_key"] = spreadsheet_engine_job_report_key(job_id)
    job["table_count"] = 1
    job["table_ids"] = [table_id]
    return save_job(job)


def run_interpret(job_id: str, *, force_ai: bool = False) -> dict[str, Any]:
    job = load_job(job_id) or {}
    if _is_reload_job(job) and not force_ai:
        return run_reload_prepare(job_id)

    job["status"] = "interpreting"
    save_job(job)
    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id))
    profile_payload = _read_json(spreadsheet_engine_job_profile_key(job_id))
    if not parse_payload or not profile_payload:
        raise ValueError(f"Missing parse/profile output for job {job_id!r}")
    report = interpret_tables(parse_payload, profile_payload)
    report["job_id"] = job_id
    _write_json(spreadsheet_engine_job_report_key(job_id), report)
    for table in report.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "")
        if table_id:
            _write_json(spreadsheet_engine_job_table_key(job_id, table_id), table)

    job = load_job(job_id) or job
    job["status"] = "interpreted"
    job["report_key"] = spreadsheet_engine_job_report_key(job_id)
    job["table_count"] = report.get("table_count", 0)
    return save_job(job)


def run_reload_finalize(job_id: str) -> dict[str, Any]:
    """Mark reload job ready after validation — no AI, no catalog write until user confirms."""
    job = load_job(job_id) or {}
    job["status"] = "proposing"
    save_job(job)

    report = load_report(job_id)
    if not report:
        raise ValueError(f"Missing report for reload job {job_id!r}")

    for table in report.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "")
        if table_id:
            _write_json(spreadsheet_engine_job_table_key(job_id, table_id), table)

    job = load_job(job_id) or job
    job["status"] = "ready"
    job["report_key"] = spreadsheet_engine_job_report_key(job_id)
    job["table_count"] = report.get("table_count", 0)
    return save_job(job)


def run_propose(job_id: str, *, force_ai: bool = False) -> dict[str, Any]:
    job = load_job(job_id) or {}
    if _is_reload_job(job) and not force_ai:
        return run_reload_finalize(job_id)

    job["status"] = "proposing"
    save_job(job)
    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id))
    profile_payload = _read_json(spreadsheet_engine_job_profile_key(job_id))
    report = load_report(job_id)
    if not parse_payload or not profile_payload or not report:
        raise ValueError(f"Missing parse/profile/report for job {job_id!r}")

    linked_catalog = None
    linked_id = str(job.get("linked_catalog_id") or "").strip().lower()
    if linked_id:
        linked_catalog = load_catalog_entry(linked_id)

    knowledge_entries: list[dict[str, Any]] = []
    for table in parse_payload.get("tables") or []:
        if not isinstance(table, dict):
            continue
        input_shape = compute_input_shape(table)
        knowledge_entries.extend(load_knowledge_matches(shape_hash=input_shape.get("shape_hash") or ""))

    table_samples: dict[str, dict[str, Any]] = {}
    filename = str(job.get("filename") or "workbook.xlsx")
    upload_key = str(job.get("upload_key") or spreadsheet_engine_job_upload_key(job_id, filename))
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(_read_bytes(upload_key))
        from hiveflow.spreadsheet.sample import extract_table_sample

        for table in parse_payload.get("tables") or []:
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "")
            if not table_id:
                continue
            table_samples[table_id] = extract_table_sample(
                local_path,
                sheet=str(table.get("sheet") or ""),
                data_start_row=int(table.get("data_start_row") or 0),
                data_end_row=int(table.get("data_end_row") or 0),
                min_col=int(table.get("min_col") or 1),
                max_col=int(table.get("max_col") or 1),
                headers=[str(name) for name in (table.get("headers") or []) if str(name).strip()],
                header_col_offsets=list(table.get("header_col_offsets") or []),
            )

    report = propose_transforms(
        parse_payload,
        profile_payload,
        report,
        linked_catalog=linked_catalog,
        knowledge_entries=knowledge_entries,
        table_samples=table_samples,
    )
    report["job_id"] = job_id
    _write_json(spreadsheet_engine_job_report_key(job_id), report)
    for table in report.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "")
        if table_id:
            _write_json(spreadsheet_engine_job_table_key(job_id, table_id), table)

    if linked_catalog:
        record_upload_on_catalog(
            linked_id,
            job_id=job_id,
            uploaded_by=str(job.get("created_by") or ""),
            input_shape_hash=_first_shape_hash(parse_payload),
        )

    job = load_job(job_id) or job
    job["status"] = "ready"
    job["report_key"] = spreadsheet_engine_job_report_key(job_id)
    job["table_count"] = report.get("table_count", 0)
    return save_job(job)


def _first_shape_hash(parse_payload: dict[str, Any]) -> str:
    for table in parse_payload.get("tables") or []:
        if isinstance(table, dict):
            return str(compute_input_shape(table).get("shape_hash") or "")
    return ""


def apply_sheet_selection(job_id: str, selected_sheets: list[str]) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    available = [str(name) for name in (job.get("sheet_names") or []) if str(name).strip()]
    chosen = [str(name).strip() for name in selected_sheets if str(name).strip()]
    if available:
        chosen = [name for name in chosen if name in set(available)]
    if not chosen:
        raise ValueError("Select at least one sheet to analyze.")
    job["selected_sheets"] = chosen
    save_job(job)
    return run_parse(job_id)


def run_pipeline(job_id: str) -> dict[str, Any]:
    try:
        job = run_parse(job_id)
        if str(job.get("status") or "") == "awaiting_sheets":
            names = [str(name) for name in (job.get("sheet_names") or []) if str(name).strip()]
            if names:
                job = apply_sheet_selection(job_id, names)
        run_profile(job_id)
        run_interpret(job_id)
        return run_propose(job_id)
    except Exception as exc:  # noqa: BLE001
        job = load_job(job_id) or {"job_id": job_id, "kind": JOB_KIND}
        job["status"] = "error"
        job["error"] = str(exc)
        return save_job(job)


def _list_table_ids(job_id: str) -> list[str]:
    prefix = f"{spreadsheet_engine_job_prefix(job_id)}/tables/"
    table_ids: list[str] = []
    bucket = _bucket()
    if bucket:
        import boto3

        client = boto3.client("s3")
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if not key.endswith(".json"):
                    continue
                name = key.rsplit("/", 1)[-1][:-5].strip().lower()
                if name:
                    table_ids.append(name)
        return sorted(dict.fromkeys(table_ids))

    root = prefix_path(_data_dir(), prefix)
    if not root.exists():
        return []
    for path in sorted(root.glob("*.json")):
        name = path.stem.strip().lower()
        if name:
            table_ids.append(name)
    return table_ids


def load_report(job_id: str) -> dict[str, Any] | None:
    report = _read_json(spreadsheet_engine_job_report_key(job_id))
    if report and report.get("tables"):
        return report
    job = load_job(job_id) or {}
    table_ids = [str(tid) for tid in (job.get("table_ids") or []) if str(tid).strip()]
    if not table_ids:
        table_ids = _list_table_ids(job_id)
    if not table_ids:
        return report
    tables = []
    for table_id in table_ids:
        table = load_table(job_id, table_id)
        if isinstance(table, dict):
            tables.append(table)
    if not tables:
        return report
    rebuilt = {
        "kind": "spreadsheet_engine_report",
        "job_id": job_id,
        "filename": job.get("filename") or (report or {}).get("filename"),
        "table_count": len(tables),
        "tables": tables,
    }
    _write_json(spreadsheet_engine_job_report_key(job_id), rebuilt)
    return rebuilt


def is_discarded_table(table: dict[str, Any] | None) -> bool:
    return str((table or {}).get("status") or "") == "discarded"


def active_proposal_tables(tables: list[Any] | None) -> list[dict[str, Any]]:
    return [
        item
        for item in (tables or [])
        if isinstance(item, dict) and not is_discarded_table(item)
    ]


def load_table(job_id: str, table_id: str) -> dict[str, Any] | None:
    return _read_json(spreadsheet_engine_job_table_key(job_id, table_id))


def load_table_preview(job_id: str, table_id: str, *, max_rows: int = 100) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.preview import MAX_PREVIEW_ROWS, extract_table_preview

    job = load_job(job_id)
    if not job:
        return None
    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id))
    if not parse_payload:
        return None
    parse_table = None
    for item in parse_payload.get("tables") or []:
        if isinstance(item, dict) and str(item.get("table_id") or "") == table_id:
            parse_table = item
            break
    if not parse_table:
        return None

    headers = [str(name) for name in (parse_table.get("headers") or []) if str(name).strip()]

    filename = str(job.get("filename") or "workbook.xlsx")
    upload_key = str(job.get("upload_key") or spreadsheet_engine_job_upload_key(job_id, filename))
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(_read_bytes(upload_key))
        preview = extract_table_preview(
            local_path,
            sheet=str(parse_table.get("sheet") or ""),
            data_start_row=int(parse_table.get("data_start_row") or 0),
            data_end_row=int(parse_table.get("data_end_row") or 0),
            min_col=int(parse_table.get("min_col") or 1),
            max_col=int(parse_table.get("max_col") or 1),
            headers=headers,
            header_col_offsets=list(parse_table.get("header_col_offsets") or []),
            max_rows=min(max_rows, MAX_PREVIEW_ROWS),
        )
    preview["table_id"] = table_id
    preview["job_id"] = job_id
    return preview


def is_discarded_job(job: dict[str, Any] | None) -> bool:
    return str((job or {}).get("status") or "") == "discarded"


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    bucket = _bucket()
    jobs: list[dict[str, Any]] = []
    if bucket:
        import boto3

        client = boto3.client("s3")
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{spreadsheet_engine_jobs_prefix()}/"):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith("/job.json"):
                    keys.append(key)
        keys.sort(reverse=True)
        for key in keys:
            payload = _read_json(key)
            if payload:
                jobs.append(payload)
    else:
        root = prefix_path(_data_dir(), spreadsheet_engine_jobs_prefix())
        if root.exists():
            for job_dir in sorted(root.iterdir(), reverse=True):
                if not job_dir.is_dir():
                    continue
                job_file = job_dir / "job.json"
                if job_file.exists():
                    payload = json.loads(job_file.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        jobs.append(payload)
    jobs.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
    return jobs[:limit]


def update_table_proposal(job_id: str, table_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    merged = {**table, **updates, "status": updates.get("status", table.get("status", "pending_review"))}
    from hiveflow.spreadsheet.stages import table_pipeline_stage

    merged["pipeline_stage"] = table_pipeline_stage(merged)
    _write_json(spreadsheet_engine_job_table_key(job_id, table_id), merged)
    report = load_report(job_id) or {"tables": []}
    tables = []
    for item in report.get("tables") or []:
        if str(item.get("table_id")) == table_id:
            tables.append(merged)
        else:
            tables.append(item)
    report["tables"] = tables
    _write_json(spreadsheet_engine_job_report_key(job_id), report)
    return merged


def update_report_tables(job_id: str, tables: list[dict[str, Any]]) -> dict[str, Any]:
    report = load_report(job_id) or {"kind": "spreadsheet_engine_report", "tables": []}
    report["tables"] = tables
    report["table_count"] = len(tables)
    _write_json(spreadsheet_engine_job_report_key(job_id), report)
    for table in tables:
        table_id = str(table.get("table_id") or "")
        if table_id:
            _write_json(spreadsheet_engine_job_table_key(job_id, table_id), table)
    return report


def catalog_id_for(job_id: str, table_id: str) -> str:
    """Legacy job-bound catalog id (backward compatibility)."""
    return f"{job_id.strip().lower()}__{table_id.strip().lower()}"


def catalog_id_for_stable(source_file_slug: str, entity_name: str) -> str:
    slug = slugify_filename(source_file_slug) if "." in source_file_slug else str(source_file_slug or "").strip().lower()
    entity = re.sub(r"[^a-z0-9_]+", "_", str(entity_name or "").strip().lower()).strip("_")
    if not slug or not entity:
        raise ValueError("source_file_slug and entity_name are required for stable catalog id")
    return f"{slug}__{entity}"


def link_job_to_catalog(job_id: str, catalog_id: str) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    cid = str(catalog_id or "").strip().lower()
    if not cid:
        raise ValueError("catalog_id is required")
    entry = load_catalog_entry(cid)
    if not entry:
        raise ValueError(f"Unknown catalog entry {cid!r}")
    job["linked_catalog_id"] = cid
    job["reupload"] = True
    return save_job(job)


def find_catalog_matches_for_parse(parse_payload: dict[str, Any], *, limit: int = 5) -> list[str]:
    """Return catalog_ids whose stored input_shape matches any parsed table."""
    matches: list[tuple[float, str]] = []
    for table in parse_payload.get("tables") or []:
        if not isinstance(table, dict):
            continue
        input_shape = compute_input_shape(table)
        shape_hash = str(input_shape.get("shape_hash") or "")
        for entry in list_catalog_entries(limit=200):
            ref_shape = entry.get("input_shape") or {}
            ref_hash = str(ref_shape.get("shape_hash") or "")
            if shape_hash and ref_hash == shape_hash:
                cid = str(entry.get("catalog_id") or "")
                if cid:
                    matches.append((1.0, cid))
            elif ref_shape:
                from hiveflow.spreadsheet.transform import shape_compatibility

                score, _ = shape_compatibility(input_shape, ref_shape)
                if score >= 0.8:
                    cid = str(entry.get("catalog_id") or "")
                    if cid:
                        matches.append((score, cid))
    seen: set[str] = set()
    ordered: list[str] = []
    for score, cid in sorted(matches, key=lambda item: -item[0]):
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
        if len(ordered) >= limit:
            break
    return ordered


def save_knowledge_entry(entry: dict[str, Any]) -> dict[str, Any]:
    kid = str(entry.get("knowledge_id") or entry.get("catalog_id") or "").strip().lower()
    if not kid:
        raise ValueError("knowledge_id is required")
    entry = {**entry, "kind": KNOWLEDGE_ENTRY_KIND, "knowledge_id": kid}
    _write_json(spreadsheet_engine_knowledge_entry_key(kid), entry)
    return entry


def load_knowledge_entry(knowledge_id: str) -> dict[str, Any] | None:
    return _read_json(spreadsheet_engine_knowledge_entry_key(knowledge_id))


def list_knowledge_entries(limit: int = 200) -> list[dict[str, Any]]:
    bucket = _bucket()
    entries: list[dict[str, Any]] = []
    if bucket:
        import boto3

        client = boto3.client("s3")
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{spreadsheet_engine_knowledge_prefix()}/"):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith(".json"):
                    keys.append(key)
        keys.sort(reverse=True)
        for key in keys[:limit]:
            payload = _read_json(key)
            if payload:
                entries.append(payload)
        return entries

    root = prefix_path(_data_dir(), spreadsheet_engine_knowledge_prefix())
    if not root.exists():
        return []
    for entry_file in sorted(root.glob("*.json"), reverse=True):
        payload = json.loads(entry_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            entries.append(payload)
        if len(entries) >= limit:
            break
    return entries


def load_knowledge_matches(
    *,
    shape_hash: str = "",
    entity_name: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    entity = str(entity_name or "").strip().lower()
    hash_val = str(shape_hash or "").strip().lower()
    for entry in list_knowledge_entries():
        keys = entry.get("match_keys") or {}
        if hash_val and str(keys.get("shape_hash") or "").lower() == hash_val:
            matches.append(entry)
        elif entity and str(entry.get("entity_name") or "").lower() == entity:
            matches.append(entry)
        if len(matches) >= limit:
            break
    return matches


def record_upload_on_catalog(
    catalog_id: str,
    *,
    job_id: str,
    uploaded_by: str = "",
    input_shape_hash: str = "",
) -> dict[str, Any]:
    entry = load_catalog_entry(catalog_id)
    if not entry:
        raise ValueError(f"Unknown catalog entry {catalog_id!r}")
    now = _now_iso()
    history = list(entry.get("upload_history") or [])
    history.append(
        {
            "job_id": job_id,
            "uploaded_at": now,
            "uploaded_by": uploaded_by,
            "input_shape_hash": input_shape_hash,
        }
    )
    entry["upload_history"] = history[-UPLOAD_HISTORY_LIMIT:]
    entry["last_upload_at"] = now
    entry["last_upload_job_id"] = job_id
    job = load_job(job_id) or {}
    if job.get("filename"):
        entry["filename"] = str(job.get("filename") or entry.get("filename") or "")
    if job.get("upload_key"):
        entry["upload_key"] = str(job.get("upload_key") or "")
    _write_json(spreadsheet_engine_catalog_entry_key(catalog_id), entry)
    return entry


def save_catalog_entry(
    job_id: str,
    table_id: str,
    table: dict[str, Any],
    *,
    filename: str = "",
    catalog_id: str = "",
    silver_materialization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job = load_job(job_id) or {}
    entity_name = str(table.get("entity_name") or table_id)
    source_slug = slugify_filename(str(filename or job.get("filename") or ""))
    cid = str(catalog_id or "").strip().lower()
    if not cid:
        try:
            cid = catalog_id_for_stable(source_slug, entity_name)
        except ValueError:
            cid = catalog_id_for(job_id, table_id)

    transformation = table.get("transformation") or {}
    input_shape = transformation.get("input_shape") or table.get("input_shape") or {}
    output_shape = transformation.get("output_shape") or build_output_shape(table)
    now = _now_iso()

    existing = load_catalog_entry(cid)
    upload_history = list((existing or {}).get("upload_history") or [])
    if not upload_history:
        upload_history = [
            {
                "job_id": job_id,
                "uploaded_at": now,
                "uploaded_by": str(table.get("approved_by") or job.get("created_by") or ""),
                "input_shape_hash": str(input_shape.get("shape_hash") or ""),
            }
        ]

    entry = {
        "kind": CATALOG_ENTRY_KIND,
        "catalog_id": cid,
        "job_id": job_id,
        "table_id": table_id,
        "legacy_catalog_id": catalog_id_for(job_id, table_id),
        "filename": filename or str(job.get("filename") or ""),
        "entity_name": entity_name,
        "source_file_slug": source_slug,
        "approved_at": table.get("approved_at"),
        "approved_by": table.get("approved_by"),
        "last_upload_at": (existing or {}).get("last_upload_at") or now,
        "last_upload_job_id": job_id,
        "upload_history": upload_history[-UPLOAD_HISTORY_LIMIT:],
        "transformation": transformation,
        "input_shape": input_shape,
        "output_shape": output_shape,
        "proposal": table,
        "upload_key": str(job.get("upload_key") or ""),
    }
    if silver_materialization:
        entry.update(silver_materialization)
    _write_json(spreadsheet_engine_catalog_entry_key(cid), entry)
    return entry


def load_catalog_entry(catalog_id: str) -> dict[str, Any] | None:
    return _read_json(spreadsheet_engine_catalog_entry_key(catalog_id))


def load_catalog_workbook(catalog_id: str) -> dict[str, Any] | None:
    """Return the original uploaded workbook for a catalog entry."""
    entry = load_catalog_entry(catalog_id)
    if not entry:
        return None
    filename = str(entry.get("filename") or "workbook.xlsx")
    job_id = str(entry.get("last_upload_job_id") or entry.get("job_id") or "").strip()
    upload_key = str(entry.get("upload_key") or "").strip()
    job = load_job(job_id) if job_id else None
    if job:
        filename = str(job.get("filename") or filename)
        if job.get("upload_key"):
            upload_key = str(job.get("upload_key") or upload_key)
    if not upload_key and job_id:
        try:
            upload_key = spreadsheet_engine_job_upload_key(job_id, filename)
        except ValueError:
            return None
    if not upload_key:
        return None
    try:
        body = _read_bytes(upload_key)
    except Exception:  # noqa: BLE001
        return None
    if not body:
        return None
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.lower().endswith(".xlsx")
        else "application/octet-stream"
    )
    return {"filename": filename, "body": body, "content_type": content_type}


def list_catalog_entries(limit: int = 100) -> list[dict[str, Any]]:
    bucket = _bucket()
    entries: list[dict[str, Any]] = []
    if bucket:
        import boto3

        client = boto3.client("s3")
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{spreadsheet_engine_catalog_prefix()}/"):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith(".json"):
                    keys.append(key)
        keys.sort(reverse=True)
        for key in keys[:limit]:
            payload = _read_json(key)
            if payload:
                entries.append(payload)
        return entries

    root = prefix_path(_data_dir(), spreadsheet_engine_catalog_prefix())
    if not root.exists():
        return []
    for entry_file in sorted(root.glob("*.json"), reverse=True):
        payload = json.loads(entry_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            entries.append(payload)
        if len(entries) >= limit:
            break
    return entries


def load_transform_preview(
    job_id: str,
    table_id: str,
    *,
    max_rows: int = 25,
) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.preview import MAX_PREVIEW_ROWS

    preview = load_table_preview(job_id, table_id, max_rows=MAX_PREVIEW_ROWS)
    if not preview:
        return None
    table = load_table(job_id, table_id) or {}
    transformation = table.get("transformation") or {}
    headers = list(preview.get("headers") or [])
    rows = list(preview.get("rows") or [])
    transform_preview = preview_transformation(rows, headers, transformation, max_rows=max_rows)
    return {
        **preview,
        "transformation_preview": transform_preview,
        "transformation_status": table.get("transformation_status"),
        "transformation_drift": list(table.get("transformation_drift") or []),
    }


def approve_transformation(
    job_id: str,
    table_id: str,
    *,
    username: str = "",
) -> dict[str, Any]:
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    transformation = table.get("transformation") or {}
    table["transformation_status"] = "approved"
    table["transformation_approved_at"] = _now_iso()
    table["transformation_approved_by"] = username
    update_table_proposal(job_id, table_id, table)

    job = load_job(job_id) or {}
    entity_name = str(table.get("entity_name") or table_id)
    source_slug = slugify_filename(str(job.get("filename") or ""))
    try:
        cid = catalog_id_for_stable(source_slug, entity_name)
    except ValueError:
        cid = catalog_id_for(job_id, table_id)

    input_shape = transformation.get("input_shape") or {}
    output_shape = transformation.get("output_shape") or build_output_shape(table)
    knowledge_entry = {
        "knowledge_id": cid,
        "catalog_id": cid,
        "entity_name": entity_name,
        "source_file_slug": source_slug,
        "input_shape": input_shape,
        "output_shape": output_shape,
        "transformation": transformation,
        "approved_at": table["transformation_approved_at"],
        "approved_by": username,
        "match_keys": {
            "shape_hash": str(input_shape.get("shape_hash") or ""),
            "headers_normalized": list(input_shape.get("headers_normalized") or []),
        },
    }
    save_knowledge_entry(knowledge_entry)

    linked_id = str(job.get("linked_catalog_id") or cid)
    if load_catalog_entry(linked_id):
        record_upload_on_catalog(
            linked_id,
            job_id=job_id,
            uploaded_by=username,
            input_shape_hash=str(input_shape.get("shape_hash") or ""),
        )
    return load_table(job_id, table_id) or table


def reject_transformation(
    job_id: str,
    table_id: str,
    *,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    """Reject transform output. With no reason, open chat; with feedback, re-synthesize."""
    from hiveflow.spreadsheet.synthesize import synthesize_from_clean_goal

    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")

    feedback = reason.strip()
    if not feedback:
        return update_table_proposal(
            job_id,
            table_id,
            {
                "transformation_status": "rejected",
                "transformation_rejected_at": _now_iso(),
                "transformation_rejected_by": username,
            },
        )

    append_table_chat(job_id, table_id, role="user", text=f"Reject transformation: {feedback}")

    clean_goal = dict(table.get("clean_goal") or {})
    if not clean_goal.get("headers") or not clean_goal.get("rows"):
        raise ValueError("Approved clean goal is required before re-proposing a transformation.")
    if str(table.get("clean_shape_status") or "") != "approved":
        raise ValueError("Approve the cleaned shape before reviewing transformations.")

    headers, rows = _load_table_sample_rows(job_id, table_id)
    synthesized = synthesize_from_clean_goal(
        headers=headers,
        rows=rows,
        clean_goal=clean_goal,
        table=table,
        feedback=feedback,
    )
    transformation = synthesized.get("transformation") or {}
    if not transformation.get("input_shape"):
        transformation["input_shape"] = compute_input_shape(
            {
                "sheet": str((table.get("source") or {}).get("sheet") or ""),
                "headers": headers,
            }
        )
    notes = list(synthesized.get("transformation_notes") or [])
    if feedback:
        notes.append(f"Re-synthesized after rejection: {feedback}")
    updates = {
        "transformation": transformation,
        "transformation_status": "pending_review",
        "transformation_confidence": synthesized.get("transformation_confidence") or 0,
        "transformation_notes": notes,
        "transformation_drift": list(synthesized.get("transformation_drift") or []),
        "induction": synthesized.get("induction") or {},
        "transformation_rejected_at": _now_iso(),
        "transformation_rejected_by": username,
    }
    updated = update_table_proposal(job_id, table_id, updates)
    append_table_chat(
        job_id,
        table_id,
        role="assistant",
        text="Re-generated deterministic steps against your approved cleaned data. Compare and approve when ready.",
    )
    return updated


def edit_transformation(
    job_id: str,
    table_id: str,
    transformation: dict[str, Any],
) -> dict[str, Any]:
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    updates = {
        "transformation": transformation,
        "transformation_status": "pending_review",
    }
    return update_table_proposal(job_id, table_id, updates)


def _load_table_sample_rows(job_id: str, table_id: str) -> tuple[list[str], list[list[Any]]]:
    """Load headers + sample rows for clean-goal synthesize / re-clean."""
    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id)) or {}
    parse_table = None
    for item in parse_payload.get("tables") or []:
        if isinstance(item, dict) and str(item.get("table_id") or "") == table_id:
            parse_table = item
            break
    if not parse_table:
        raise ValueError(f"Unknown parse table {table_id!r} for job {job_id!r}")

    headers = [str(name) for name in (parse_table.get("headers") or []) if str(name).strip()]
    job = load_job(job_id) or {}
    filename = str(job.get("filename") or "workbook.xlsx")
    upload_key = str(job.get("upload_key") or spreadsheet_engine_job_upload_key(job_id, filename))
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(_read_bytes(upload_key))
        from hiveflow.spreadsheet.sample import extract_table_sample

        sample = extract_table_sample(
            local_path,
            sheet=str(parse_table.get("sheet") or ""),
            data_start_row=int(parse_table.get("data_start_row") or 0),
            data_end_row=int(parse_table.get("data_end_row") or 0),
            min_col=int(parse_table.get("min_col") or 1),
            max_col=int(parse_table.get("max_col") or 1),
            headers=headers,
            header_col_offsets=list(parse_table.get("header_col_offsets") or []),
        )
    rows = list(sample.get("rows") or [])
    if not rows:
        rows = list(parse_table.get("sample_rows") or [])
    return headers, rows


def approve_clean_shape(
    job_id: str,
    table_id: str,
    *,
    username: str = "",
) -> dict[str, Any]:
    """Lock the cleaned table as the final goal and synthesize deterministic steps."""
    from hiveflow.spreadsheet.synthesize import synthesize_from_clean_goal

    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    clean_goal = dict(table.get("clean_goal") or {})
    if not clean_goal.get("headers") or not clean_goal.get("rows"):
        raise ValueError("Clean goal is missing headers/rows — re-run propose first.")

    headers, rows = _load_table_sample_rows(job_id, table_id)
    synthesized = synthesize_from_clean_goal(
        headers=headers,
        rows=rows,
        clean_goal=clean_goal,
        table=table,
    )
    transformation = synthesized.get("transformation") or {}
    input_shape = compute_input_shape(
        {
            "sheet": str((table.get("source") or {}).get("sheet") or ""),
            "headers": headers,
        }
    )
    if not transformation.get("input_shape"):
        transformation["input_shape"] = input_shape

    clean_goal["final"] = True
    updates = {
        "clean_goal": clean_goal,
        "clean_shape_status": "approved",
        "clean_shape_approved_at": _now_iso(),
        "clean_shape_approved_by": username,
        "transformation": transformation,
        "transformation_status": synthesized.get("transformation_status") or "pending_review",
        "transformation_confidence": synthesized.get("transformation_confidence") or 0,
        "transformation_notes": list(synthesized.get("transformation_notes") or []),
        "transformation_drift": list(synthesized.get("transformation_drift") or []),
        "induction": synthesized.get("induction") or {},
    }
    if clean_goal.get("grain"):
        updates["grain"] = clean_goal["grain"]
    goal_schema = (transformation.get("output_shape") or {}).get("schema")
    if goal_schema:
        updates["schema"] = goal_schema
    return update_table_proposal(job_id, table_id, updates)


def reject_clean_shape(
    job_id: str,
    table_id: str,
    *,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    """Reject the cleaned shape. With no reason, open chat; with feedback, re-clean."""
    from hiveflow.spreadsheet.synthesize import propose_clean_goal

    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")

    feedback = reason.strip()
    if not feedback:
        return update_table_proposal(
            job_id,
            table_id,
            {
                "clean_shape_status": "rejected",
                "clean_shape_rejected_at": _now_iso(),
                "clean_shape_rejected_by": username,
            },
        )

    append_table_chat(job_id, table_id, role="user", text=f"Reject cleaned shape: {feedback}")

    headers, rows = _load_table_sample_rows(job_id, table_id)
    prior = dict(table.get("clean_goal") or {})
    proposal = propose_clean_goal(
        headers=headers,
        rows=rows,
        table=table,
        feedback=feedback,
        prior_goal=prior,
    )
    updates = {
        **proposal,
        "clean_shape_status": "pending_review",
        "clean_shape_rejected_at": _now_iso(),
        "clean_shape_rejected_by": username,
    }
    if feedback:
        notes = list(updates.get("clean_shape_notes") or [])
        notes.append(f"Re-cleaned after rejection: {feedback}")
        updates["clean_shape_notes"] = notes
    updated = update_table_proposal(job_id, table_id, updates)
    append_table_chat(
        job_id,
        table_id,
        role="assistant",
        text="Updated the cleaned shape based on your feedback. Review and approve when ready.",
    )
    return updated


def reject_job(
    job_id: str,
    *,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    """Remove a workbook upload from proposal review without deleting catalogued tables."""
    del reason
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    if is_discarded_job(job):
        return job
    report = load_report(job_id) or {}
    for item in report.get("tables") or []:
        if not isinstance(item, dict):
            continue
        table_id = str(item.get("table_id") or "").strip()
        status = str(item.get("status") or "")
        if not table_id or status in {"approved", "discarded"}:
            continue
        reject_table(job_id, table_id, username=username)
    job["status"] = "discarded"
    job["discarded_at"] = _now_iso()
    job["discarded_by"] = username
    return save_job(job)


def reject_table(
    job_id: str,
    table_id: str,
    *,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    """Remove a table from proposal review without cataloguing it."""
    del reason
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    if str(table.get("status") or "") == "approved":
        raise ValueError("Approved tables cannot be removed from consideration.")
    return update_table_proposal(
        job_id,
        table_id,
        {
            "status": "discarded",
            "discarded_at": _now_iso(),
            "discarded_by": username,
        },
    )


def _materialize_table_for_job(job_id: str, table_id: str, table: dict[str, Any]) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.materialize import materialization_payload, materialize_approved_table

    job = load_job(job_id) or {}
    parse_payload = _read_json(spreadsheet_engine_job_parse_key(job_id))
    if not parse_payload:
        return None
    filename = str(job.get("filename") or "workbook.xlsx")
    upload_key = str(job.get("upload_key") or spreadsheet_engine_job_upload_key(job_id, filename))
    try:
        upload_body = _read_bytes(upload_key)
    except Exception:  # noqa: BLE001
        return None
    result = materialize_approved_table(
        job=job,
        table={**table, "table_id": table_id},
        parse_payload=parse_payload,
        upload_body=upload_body,
    )
    if not result:
        return None
    return materialization_payload(result, materialized_at=_now_iso())


def approve_table(job_id: str, table_id: str, *, username: str = "") -> dict[str, Any]:
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")

    if str(table.get("status") or "") == "discarded":
        raise ValueError("Discarded tables cannot be approved.")

    shape_status = str(table.get("clean_shape_status") or "")
    if table.get("clean_goal") and shape_status not in {"", "approved"}:
        raise ValueError("Approve the cleaned shape before approving the table.")

    transformation = table.get("transformation") or {}
    steps = transformation.get("steps") or []
    transform_status = str(table.get("transformation_status") or "")
    if steps and transform_status != "approved":
        raise ValueError("Approve the transformation before approving the table.")

    table["status"] = "approved"
    table["approved_at"] = _now_iso()
    table["approved_by"] = username
    _write_json(spreadsheet_engine_job_table_key(job_id, table_id), table)
    report = load_report(job_id) or {"tables": []}
    tables = []
    for item in report.get("tables") or []:
        if str(item.get("table_id")) == table_id:
            tables.append(table)
        else:
            tables.append(item)
    report["tables"] = tables
    _write_json(spreadsheet_engine_job_report_key(job_id), report)
    job = load_job(job_id) or {}
    silver_materialization = _materialize_table_for_job(job_id, table_id, table)
    entry = save_catalog_entry(
        job_id,
        table_id,
        table,
        filename=str(job.get("filename") or ""),
        silver_materialization=silver_materialization,
    )
    if transformation and steps:
        save_knowledge_entry(
            {
                "knowledge_id": entry["catalog_id"],
                "catalog_id": entry["catalog_id"],
                "entity_name": entry.get("entity_name"),
                "source_file_slug": entry.get("source_file_slug"),
                "input_shape": entry.get("input_shape"),
                "output_shape": entry.get("output_shape"),
                "transformation": transformation,
                "approved_at": table["approved_at"],
                "approved_by": username,
                "match_keys": {
                    "shape_hash": str((entry.get("input_shape") or {}).get("shape_hash") or ""),
                    "headers_normalized": list(
                        (entry.get("input_shape") or {}).get("headers_normalized") or []
                    ),
                },
            }
        )
    table["silver_entity"] = entry.get("silver_entity")
    table["silver_source"] = entry.get("silver_source")
    return table


def complete_reload(job_id: str, table_id: str, *, username: str = "") -> dict[str, Any]:
    """Complete a passed reload — update catalog without re-running AI."""
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    if str(table.get("reload_validation_status") or "") != "passed":
        raise ValueError("Reload validation must pass before completing the reload.")
    job = load_job(job_id) or {}
    linked_id = str(job.get("linked_catalog_id") or "").strip().lower()
    if not linked_id:
        raise ValueError("Linked catalog entry is required to complete reload.")

    table["status"] = "approved"
    table["approved_at"] = _now_iso()
    table["approved_by"] = username
    update_table_proposal(job_id, table_id, table)

    silver_materialization = _materialize_table_for_job(job_id, table_id, table)
    entry = save_catalog_entry(
        job_id,
        table_id,
        table,
        filename=str(job.get("filename") or ""),
        catalog_id=linked_id,
        silver_materialization=silver_materialization,
    )
    record_upload_on_catalog(
        linked_id,
        job_id=job_id,
        uploaded_by=username,
        input_shape_hash=str((table.get("transformation") or {}).get("input_shape", {}).get("shape_hash") or ""),
    )
    return {"table": table, "catalog_entry": entry}


def request_schema_rewrite(job_id: str) -> dict[str, Any]:
    """Re-run interpret + propose with AI after a failed reload validation."""
    job = load_job(job_id) or {}
    job["reupload"] = False
    job["reload_mode"] = False
    job["reload_validation_status"] = ""
    save_job(job)
    run_interpret(job_id, force_ai=True)
    return run_propose(job_id, force_ai=True)


def request_transformation_rewrite(job_id: str) -> dict[str, Any]:
    """Re-run transformation proposal with AI, keeping the current schema."""
    job = load_job(job_id) or {}
    job["reload_mode"] = False
    job["reload_validation_status"] = ""
    save_job(job)
    return run_propose(job_id, force_ai=True)


def append_table_chat(job_id: str, table_id: str, *, role: str, text: str) -> dict[str, Any]:
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    history = list(table.get("chat_history") or [])
    history.append({"role": role, "text": text, "at": _now_iso()})
    table["chat_history"] = history[-20:]
    return update_table_proposal(job_id, table_id, {"chat_history": table["chat_history"]})


def append_chat(job_id: str, *, role: str, text: str) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    history = list(job.get("chat_history") or [])
    history.append({"role": role, "text": text, "at": _now_iso()})
    job["chat_history"] = history[-20:]
    return save_job(job)
