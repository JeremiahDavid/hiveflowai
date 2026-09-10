"""Spreadsheet Engine portal service — uploads, pipeline kickoff, chat, approvals."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from hiveflow.compat import UTC

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.web.portal.governance_helpers.bedrock_usage import (
    BedrockBudgetExceeded,
    usage_summary,
)
from hiveflow.process_config import Process, step_function_name_for_process

_PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    ("parse", "Parse workbook"),
    ("profile", "Profile columns"),
    ("interpret", "Generate proposals"),
    ("propose", "Propose transformations"),
    ("ready", "Ready for review"),
)

_COMPLETED_STAGE_COUNT: dict[str, int] = {
    "uploaded": 0,
    "running": 0,
    "parsing": 0,
    "parsed": 1,
    "profiling": 1,
    "profiled": 2,
    "interpreting": 2,
    "interpreted": 3,
    "proposing": 3,
    "ready": 5,
}

_ACTIVE_STAGE_INDEX: dict[str, int] = {
    "uploaded": 0,
    "running": 0,
    "parsing": 0,
    "parsed": 1,
    "profiling": 1,
    "profiled": 2,
    "interpreting": 2,
    "interpreted": 3,
    "proposing": 3,
    "ready": -1,
}


_RELOAD_PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    ("parse", "Parse workbook"),
    ("profile", "Profile columns"),
    ("interpret", "Validate against catalog"),
    ("propose", "Finalize validation"),
    ("ready", "Ready for review"),
)


def spreadsheet_pipeline_progress(
    job_status: str,
    *,
    execution_status: str = "",
    error: str = "",
    reload_mode: bool = False,
) -> dict[str, Any]:
    pipeline_stages = _RELOAD_PIPELINE_STAGES if reload_mode else _PIPELINE_STAGES
    status = str(job_status or "uploaded").strip().lower()
    execution = str(execution_status or "").strip().lower()
    err = str(error or "").strip()
    stages: list[dict[str, str]] = []
    complete = status == "ready"
    failed = status == "error" or execution in {"failed", "timed_out", "aborted"}

    if failed:
        active_index = max(_ACTIVE_STAGE_INDEX.get(status, 0), 0)
        for index, (key, label) in enumerate(pipeline_stages):
            if index < active_index:
                state = "complete"
            elif index == active_index:
                state = "error"
            else:
                state = "pending"
            stages.append({"key": key, "label": label, "state": state})
        label, detail = "Analysis failed", err or "The Step Functions workflow did not complete successfully."
        return {
            "job_status": status,
            "execution_status": execution,
            "status_label": label,
            "status_detail": detail,
            "error": err,
            "stages": stages,
            "complete": False,
            "failed": True,
        }

    if complete:
        for key, label in pipeline_stages:
            stages.append({"key": key, "label": label, "state": "complete"})
        return {
            "job_status": status,
            "execution_status": execution or "succeeded",
            "status_label": (
                "Approved steps applied" if reload_mode else "Cleaned proposals ready"
            ),
            "status_detail": (
                "Approved steps for this workbook were applied — review and give final approval."
                if reload_mode
                else "Review the cleaned table proposals below."
            ),
            "error": "",
            "stages": stages,
            "complete": True,
            "failed": False,
        }

    completed_count = _COMPLETED_STAGE_COUNT.get(status, 0)
    active_index = _ACTIVE_STAGE_INDEX.get(status, 0)
    for index, (key, label) in enumerate(pipeline_stages):
        if index < completed_count:
            state = "complete"
        elif index == active_index:
            state = "active"
        else:
            state = "pending"
        stages.append({"key": key, "label": label, "state": state})

    if reload_mode:
        label = "Applying approved steps"
        detail = "Approved steps for this workbook are being applied for final approval."
    else:
        label = "Generating cleaned proposals"
        detail = "AI is generating a cleaned proposal of all tables in this workbook."

    if execution == "running":
        detail = f"{detail} Step Functions status: RUNNING."

    return {
        "job_status": status,
        "execution_status": execution,
        "status_label": label,
        "status_detail": detail,
        "error": "",
        "stages": stages,
        "complete": False,
        "failed": False,
    }


def _on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip())


def _state_machine_arn(*, company: str, environment: str) -> str:
    explicit = os.getenv("HIVEFLOW_SPREADSHEET_STATE_MACHINE_ARN", "").strip()
    if explicit:
        return explicit
    name = step_function_name_for_process(company, environment, "all", Process.SPREADSHEET_ANALYZE)
    region = (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("HIVEFLOW_AWS_REGION")
        or "us-east-2"
    )
    import boto3

    account = boto3.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:states:{region}:{account}:stateMachine:{name}"


def _configure_jobs_env(settings: DnaSettings) -> None:
    if settings.s3_bucket:
        os.environ["HIVEFLOW_S3_BUCKET"] = settings.s3_bucket
    if settings.data_dir:
        os.environ["HIVEFLOW_DATA_DIR"] = str(settings.data_dir)


def load_job_report(settings: DnaSettings, *, job_id: str) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.jobs import load_report

    _configure_jobs_env(settings)
    return load_report(job_id)


def load_table_preview_data(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    max_rows: int = 100,
) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.jobs import load_table_preview

    _configure_jobs_env(settings)
    if not job_id.strip() or not table_id.strip():
        return None
    return load_table_preview(job_id.strip(), table_id.strip(), max_rows=max_rows)


def start_upload(
    settings: DnaSettings,
    *,
    filename: str,
    body: bytes,
    username: str = "",
    linked_catalog_id: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import create_job, store_upload

    _configure_jobs_env(settings)
    job = create_job(
        filename=filename,
        username=username,
        linked_catalog_id=linked_catalog_id,
    )
    store_upload(job["job_id"], filename=filename, body=body)
    return job


def parse_upload(
    settings: DnaSettings,
    *,
    job_id: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import run_parse

    _configure_jobs_env(settings)
    return run_parse(job_id)


def select_sheets(
    settings: DnaSettings,
    *,
    job_id: str,
    sheets: list[str],
    company: str,
    environment: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import apply_sheet_selection

    _configure_jobs_env(settings)
    apply_sheet_selection(job_id, sheets)
    return enqueue_analysis(
        settings,
        job_id=job_id,
        company=company,
        environment=environment,
    )


def enqueue_analysis(
    settings: DnaSettings,
    *,
    job_id: str,
    company: str,
    environment: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import load_job, run_pipeline, save_job

    _configure_jobs_env(settings)
    job = load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    if str(job.get("status") or "") == "awaiting_sheets" and not job.get("selected_sheets"):
        raise ValueError("Select which sheets to analyze before generating proposals.")

    payload = {"job_id": job_id}
    if _on_lambda():
        import boto3

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
        client = boto3.client("stepfunctions", region_name=region)
        response = client.start_execution(
            stateMachineArn=_state_machine_arn(company=company, environment=environment),
            input=json.dumps(payload),
        )
        job["status"] = "running"
        job["execution_arn"] = response.get("executionArn", "")
        save_job(job)
        return {
            "status": "enqueued",
            "job_id": job_id,
            "execution_arn": job.get("execution_arn"),
        }

    job = run_pipeline(job_id)
    return {"status": job.get("status", "ready"), "job_id": job_id, "job": job}


def job_status(
    settings: DnaSettings,
    *,
    job_id: str,
    company: str,
    environment: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import load_job, load_report, save_job

    _configure_jobs_env(settings)
    job = load_job(job_id)
    if not job:
        report = load_report(job_id)
        return {
            "status": "missing",
            "job_id": job_id,
            "job": None,
            "report": report,
            "table_count": (report or {}).get("table_count", 0),
            "execution_arn": "",
            "execution_status": "",
            "pipeline": spreadsheet_pipeline_progress(
                "ready" if report and report.get("tables") else "error",
                error="" if report and report.get("tables") else "Workbook job not found.",
            ),
        }
    execution_arn = str(job.get("execution_arn") or "")
    execution_status = ""
    execution_error = ""
    if execution_arn:
        import boto3

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
        client = boto3.client("stepfunctions", region_name=region)
        try:
            execution = client.describe_execution(executionArn=execution_arn)
            sf_status = str(execution.get("status") or "")
            execution_status = sf_status.strip().lower()
            changed = False
            if sf_status == "SUCCEEDED":
                pass
            elif sf_status in {"FAILED", "TIMED_OUT", "ABORTED"}:
                execution_error = str(execution.get("cause") or execution.get("error") or sf_status)
                if str(job.get("status") or "") != "error" or job.get("error") != execution_error:
                    job["status"] = "error"
                    job["error"] = execution_error
                    changed = True
            elif str(job.get("status") or "") in {"", "uploaded"}:
                job["status"] = "running"
                changed = True
            if changed:
                save_job(job)
        except Exception:  # noqa: BLE001
            pass
    report = load_report(job_id)
    job_status_now = str(job.get("status") or "")
    in_flight = {
        "uploaded",
        "running",
        "parsing",
        "parsed",
        "profiling",
        "profiled",
        "interpreting",
        "interpreted",
        "proposing",
    }
    # Interpret writes tables before propose attaches clean_goal. Do not treat that as ready.
    if (
        report
        and report.get("tables")
        and job_status_now not in {"error", "awaiting_sheets", "discarded", *in_flight}
        and job_status_now != "ready"
    ):
        job["status"] = "ready"
        save_job(job)
    elif execution_status == "succeeded" and job_status_now not in {"error", "ready", "discarded"} and job_status_now not in in_flight:
        job["status"] = "ready"
        save_job(job)
    elif execution_status == "succeeded" and job_status_now == "proposing":
        # Propose lambda may still be finishing; keep polling unless clean goals exist.
        tables = list((report or {}).get("tables") or [])
        if tables and all((t or {}).get("clean_goal") for t in tables if isinstance(t, dict)):
            job["status"] = "ready"
            save_job(job)
    job_status_value = str(job.get("status") or "")
    reload_mode = bool(
        (job or {}).get("reload_mode")
        or (job or {}).get("reupload")
        or (job or {}).get("linked_catalog_id")
    )
    pipeline = spreadsheet_pipeline_progress(
        job_status_value,
        execution_status=execution_status,
        error=str(job.get("error") or execution_error or ""),
        reload_mode=reload_mode,
    )
    return {
        "status": job_status_value,
        "job_id": job_id,
        "job": job,
        "report": report,
        "table_count": (report or {}).get("table_count", 0),
        "execution_arn": execution_arn,
        "execution_status": execution_status,
        "pipeline": pipeline,
    }


def list_proposal_jobs(settings: DnaSettings, *, limit: int = 50) -> list[dict[str, Any]]:
    from hiveflow.spreadsheet.jobs import is_discarded_job, list_jobs

    _configure_jobs_env(settings)
    return [job for job in list_jobs(limit=limit) if not is_discarded_job(job)]


def reject_job(
    settings: DnaSettings,
    *,
    job_id: str,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import reject_job as _reject

    _configure_jobs_env(settings)
    return _reject(job_id, reason=reason, username=username)


def list_catalog_entries(settings: DnaSettings, *, limit: int = 100) -> list[dict[str, Any]]:
    from hiveflow.spreadsheet.jobs import list_catalog_entries as _list

    _configure_jobs_env(settings)
    return _list(limit=limit)


def load_catalog_entry(settings: DnaSettings, *, catalog_id: str) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.jobs import load_catalog_entry as _load

    _configure_jobs_env(settings)
    return _load(catalog_id)


def load_catalog_workbook(
    settings: DnaSettings,
    *,
    catalog_id: str,
) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.jobs import load_catalog_workbook as _load

    _configure_jobs_env(settings)
    if not catalog_id.strip():
        return None
    return _load(catalog_id.strip())


def load_transform_preview_data(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    max_rows: int = 25,
) -> dict[str, Any] | None:
    from hiveflow.spreadsheet.jobs import load_transform_preview

    _configure_jobs_env(settings)
    if not job_id.strip() or not table_id.strip():
        return None
    return load_transform_preview(job_id.strip(), table_id.strip(), max_rows=max_rows)


def link_job_catalog(
    settings: DnaSettings,
    *,
    job_id: str,
    catalog_id: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import link_job_to_catalog

    _configure_jobs_env(settings)
    return link_job_to_catalog(job_id, catalog_id)


def suggest_catalog_matches(
    settings: DnaSettings,
    *,
    job_id: str,
) -> list[str]:
    from hiveflow.spreadsheet.jobs import find_catalog_matches_for_parse, load_job
    from hiveflow.storage.paths import spreadsheet_engine_job_parse_key

    _configure_jobs_env(settings)
    job = load_job(job_id)
    if not job:
        return []
    import json
    import os
    from pathlib import Path

    from hiveflow.storage.paths import prefix_path

    parse_key = spreadsheet_engine_job_parse_key(job_id)
    parse_payload = None
    bucket = os.getenv("HIVEFLOW_S3_BUCKET", "").strip()
    if bucket:
        from hiveflow.storage.aws import s3_client

        try:
            response = s3_client().get_object(Bucket=bucket, Key=parse_key)
            parse_payload = json.loads(response["Body"].read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            parse_payload = None
    else:
        data_dir = Path(os.getenv("HIVEFLOW_DATA_DIR", "data")).resolve()
        path = prefix_path(data_dir, parse_key)
        if path.exists():
            parse_payload = json.loads(path.read_text(encoding="utf-8"))
    if not parse_payload:
        return list(job.get("suggested_catalog_ids") or [])
    return find_catalog_matches_for_parse(parse_payload)


def approve_transformation(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import approve_transformation as _approve

    _configure_jobs_env(settings)
    return _approve(job_id, table_id, username=username)


def approve_clean_shape(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import approve_clean_shape as _approve

    _configure_jobs_env(settings)
    return _approve(job_id, table_id, username=username)


def reject_clean_shape(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import reject_clean_shape as _reject

    _configure_jobs_env(settings)
    return _reject(job_id, table_id, reason=reason, username=username)


def reject_transformation(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import reject_transformation as _reject

    _configure_jobs_env(settings)
    return _reject(job_id, table_id, reason=reason, username=username)


def reject_table(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import reject_table as _reject

    _configure_jobs_env(settings)
    return _reject(job_id, table_id, reason=reason, username=username)


def edit_transformation(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    transformation: dict[str, Any],
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import edit_transformation as _edit

    _configure_jobs_env(settings)
    return _edit(job_id, table_id, transformation)


def reupload_to_catalog(
    settings: DnaSettings,
    *,
    catalog_id: str,
    filename: str,
    body: bytes,
    username: str = "",
    company: str,
    environment: str,
) -> dict[str, Any]:
    job = start_upload(
        settings,
        filename=filename,
        body=body,
        username=username,
        linked_catalog_id=catalog_id,
    )
    result = enqueue_analysis(
        settings,
        job_id=job["job_id"],
        company=company,
        environment=environment,
    )
    return {"job": job, **result}


def _attach_join_proposals(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    table: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from hiveflow.dna.join_proposals import propose_joins_for_table
    from hiveflow.spreadsheet.jobs import load_table, update_table_proposal

    current = table or load_table(job_id, table_id) or {}
    try:
        payload = propose_joins_for_table(current, settings=settings)
        notes = list(payload.get("notes") or [])
    except Exception as exc:  # noqa: BLE001
        payload = {"proposals": [], "notes": [f"DNA join proposal failed: {exc}"]}
        notes = list(payload["notes"])
    proposals = list(payload.get("proposals") or [])
    for item in proposals:
        item["selected"] = True
    return update_table_proposal(
        job_id,
        table_id,
        {
            "status": str(current.get("status") or "approved"),
            "join_proposals": proposals,
            "join_status": "pending_review",
            "join_notes": notes,
            "join_source_keys": list(payload.get("source_keys") or []),
        },
    )


def _attach_data_profile(
    settings: DnaSettings,
    *,
    table: dict[str, Any],
) -> None:
    """Run the newly materialized reference table through the data profiling engine.

    Best-effort: profiling failures never block table approval.
    """
    silver_entity = str(table.get("silver_entity") or "").strip()
    silver_source = str(table.get("silver_source") or "").strip()
    if not silver_entity or not silver_source:
        return
    from hiveflow.dna.data_profile import profile_entity

    try:
        profile_entity(settings, silver_source, silver_entity, layer="silver")
    except Exception:  # noqa: BLE001
        pass


def approve_table(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import approve_table as _approve

    _configure_jobs_env(settings)
    table = _approve(job_id, table_id, username=username)
    _attach_data_profile(settings, table=table)
    return _attach_join_proposals(settings, job_id=job_id, table_id=table_id, table=table)


def complete_reload(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import complete_reload as _complete

    _configure_jobs_env(settings)
    result = _complete(job_id, table_id, username=username)
    table = _attach_join_proposals(settings, job_id=job_id, table_id=table_id)
    if isinstance(result, dict):
        result["table"] = table
        return result
    return table


def refresh_joins(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
) -> dict[str, Any]:
    _configure_jobs_env(settings)
    return _attach_join_proposals(settings, job_id=job_id, table_id=table_id)


def approve_joins(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    selected_ids: list[str] | None = None,
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import load_table, update_table_proposal

    _configure_jobs_env(settings)
    table = load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    if str(table.get("status") or "") != "approved":
        raise ValueError("Catalog the table before approving joins.")
    wanted = {str(item).strip() for item in (selected_ids or []) if str(item).strip()}
    proposals = []
    for item in table.get("join_proposals") or []:
        if not isinstance(item, dict):
            continue
        updated = dict(item)
        if wanted:
            updated["selected"] = str(item.get("id") or "") in wanted
        else:
            updated["selected"] = False
        proposals.append(updated)
    return update_table_proposal(
        job_id,
        table_id,
        {
            "status": "approved",
            "join_proposals": proposals,
            "join_status": "approved",
            "join_approved_at": datetime.now(UTC).isoformat(),
            "join_approved_by": username,
        },
    )


def reject_joins(
    settings: DnaSettings,
    *,
    job_id: str,
    table_id: str,
    reason: str = "",
    username: str = "",
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import load_table, update_table_proposal

    _configure_jobs_env(settings)
    table = load_table(job_id, table_id) or {}
    notes = list(table.get("join_notes") or [])
    text = str(reason or "").strip()
    if text:
        notes.append(text)
    update_table_proposal(
        job_id,
        table_id,
        {
            "status": str(table.get("status") or "approved"),
            "join_status": "rejected",
            "join_notes": notes,
            "join_rejected_by": username,
        },
    )
    return _attach_join_proposals(settings, job_id=job_id, table_id=table_id)


def request_schema_rewrite(
    settings: DnaSettings,
    *,
    job_id: str,
    company: str,
    environment: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import request_schema_rewrite as _rewrite

    _configure_jobs_env(settings)
    job = _rewrite(job_id)
    return {"job": job, "job_id": job_id}


def request_transformation_rewrite(
    settings: DnaSettings,
    *,
    job_id: str,
    company: str,
    environment: str,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.jobs import request_transformation_rewrite as _rewrite

    _configure_jobs_env(settings)
    job = _rewrite(job_id)
    return {"job": job, "job_id": job_id}


def chat_feedback(
    settings: DnaSettings,
    *,
    job_id: str,
    message: str,
    table_id: str = "",
    client_id: str = "",
    monthly_budget_usd: float | None = None,
) -> dict[str, Any]:
    from hiveflow.spreadsheet.interpret import _default_invoke, _extract_json
    from hiveflow.spreadsheet.jobs import (
        append_table_chat,
        load_report,
        load_table,
        reject_clean_shape as _reject_clean_shape,
        reject_transformation as _reject_transformation,
        update_table_proposal,
    )

    _configure_jobs_env(settings)
    text = message.strip()
    if not text:
        raise ValueError("message is required")
    if not table_id.strip():
        raise ValueError("table_id is required to refine a proposal")

    table_id = table_id.strip()
    focus = load_table(job_id, table_id)
    if not focus:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")

    shape_status = str(focus.get("clean_shape_status") or "")
    transform_status = str(focus.get("transformation_status") or "")
    if shape_status == "rejected":
        _reject_clean_shape(job_id, table_id, reason=text, username="")
        return {"reply": "", "report": load_report(job_id), "table": load_table(job_id, table_id)}
    if transform_status == "rejected":
        _reject_transformation(job_id, table_id, reason=text, username="")
        return {"reply": "", "report": load_report(job_id), "table": load_table(job_id, table_id)}

    append_table_chat(job_id, table_id, role="user", text=text)

    report = load_report(job_id) or {}
    tables = report.get("tables") or []
    context = {
        "job_id": job_id,
        "user_message": text,
        "current_proposal": focus,
        "focus_table": focus,
        "other_tables": [t for t in tables if str(t.get("table_id")) != table_id],
    }
    system = (
        "You help refine one spreadsheet table proposal at a time. "
        "current_proposal / focus_table is the operator's current proposal — "
        "keep it in context when applying feedback. "
        "If clean_shape_status is pending_review or rejected, prefer updating clean_goal "
        "(headers/rows/grain/notes) rather than transformation steps. "
        "Only update the focused table unless the user explicitly asks about others. "
        "Return JSON: {\"assistant_reply\": \"...\", \"table_updates\": "
        "[{\"table_id\":\"t0\",\"entity_name\":\"...\",\"purpose\":\"...\","
        "\"grain\":\"...\",\"schema\":[...],\"clean_goal\":{...},"
        "\"transformation\":{...},\"notes\":[\"...\"]}]}"
    )
    reply = "Updated this table proposal based on your feedback."
    try:
        raw = _default_invoke(system, json.dumps(context, default=str))
        parsed = _extract_json(raw)
        reply = str(parsed.get("assistant_reply") or reply)
        updates = parsed.get("table_updates") or []
        if isinstance(updates, list):
            for item in updates:
                if not isinstance(item, dict):
                    continue
                tid = str(item.get("table_id") or "")
                if tid:
                    if item.get("clean_goal"):
                        item["clean_shape_status"] = "pending_review"
                        item["transformation_status"] = "awaiting_shape"
                        item.setdefault("transformation", {"version": 1, "steps": []})
                    if item.get("transformation") and item.get("transformation", {}).get("steps"):
                        item["transformation_status"] = "pending_review"
                    if str(focus.get("status") or "") == "rejected":
                        item["status"] = "pending_review"
                    update_table_proposal(job_id, tid, item)
    except BedrockBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        reply = f"I could not apply that change automatically ({exc}). Try rephrasing your feedback."
    append_table_chat(job_id, table_id, role="assistant", text=reply)
    return {"reply": reply, "report": load_report(job_id), "table": load_table(job_id, table_id)}


def bedrock_usage_for_client(
    settings: DnaSettings,
    *,
    client_id: str,
    monthly_budget_usd: float | None,
) -> dict[str, Any]:
    return usage_summary(
        settings,
        client_id=client_id,
        monthly_budget_usd=monthly_budget_usd,
    ).to_dict()
