"""Async, no-Step-Functions task dispatch for Spreadsheet Lab.

A per-table AI call can legitimately take 10-60+ seconds — too long to run
inline inside the request that triggered it (API Gateway's ~29s hard limit).
Instead of a full Step Functions state machine, this reuses the simpler
pattern already shipping in the production portal's KPI Generator
(``hiveflow.dna.web.portal.kpi_generator.generation.enqueue_kpi_generation``):
the HTTP-serving Lambda invocation writes a "processing" stub and returns
immediately, then asynchronously self-invokes the *same* Lambda function with
a ``hiveflow_task`` marker; that second, separate invocation does the real
work with its own full timeout budget. Locally (no ``AWS_LAMBDA_FUNCTION_NAME``)
the work just runs inline instead.

Spreadsheet Lab is single-tenant (its Lambda's env vars are set once, statically,
at deploy time — see ``infra/stacks/spreadsheet_lab_stack.py``), so unlike the
KPI Generator's worker there is no per-event tenant rebinding to do.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

TABLE_TASK = "spreadsheet_lab_run_table"

TASK_EXTRACT_VALIDATE = "extract_validate"
TASK_EXTRACT_RETRY = "extract_retry"
TASK_CLEAN_PROPOSE = "clean_propose"
TASK_CLEAN_RETRY_SHAPE = "clean_retry_shape"
TASK_CLEAN_SYNTHESIZE = "clean_synthesize"
TASK_CLEAN_RETRY_TRANSFORM = "clean_retry_transform"


def _on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip())


def _invoke_self_async(payload: dict[str, Any]) -> None:
    import boto3

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    if not function_name:
        raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME is not set")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
    client = boto3.client("lambda", region_name=region)
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload, default=str).encode("utf-8"),
    )
    status = int(response.get("StatusCode") or 0)
    if status not in {202, 200}:
        raise RuntimeError(f"Spreadsheet Lab table task invoke returned status {status}")


def _dispatch(job_id: str, table_id: str, task: str, **kwargs: Any) -> None:
    from hiveflow.spreadsheet_lab import store

    table = store.load_table(job_id, table_id)
    if table:
        table["status"] = "processing"
        store.save_table(table)

    payload = {
        "hiveflow_task": TABLE_TASK,
        "job_id": job_id,
        "table_id": table_id,
        "task": task,
        **kwargs,
    }
    if _on_lambda():
        _invoke_self_async(payload)
    else:
        run_table_task(payload)


def enqueue_extract_validate(job_id: str, table_id: str, *, invoke: Any = None) -> None:
    _dispatch(job_id, table_id, TASK_EXTRACT_VALIDATE, invoke=invoke)


def enqueue_extract_retry(job_id: str, table_id: str, *, feedback: str, invoke: Any = None) -> None:
    _dispatch(job_id, table_id, TASK_EXTRACT_RETRY, feedback=feedback, invoke=invoke)


def enqueue_clean_propose(job_id: str, table_id: str, *, invoke: Any = None) -> None:
    _dispatch(job_id, table_id, TASK_CLEAN_PROPOSE, invoke=invoke)


def enqueue_clean_retry_shape(job_id: str, table_id: str, *, feedback: str, invoke: Any = None) -> None:
    _dispatch(job_id, table_id, TASK_CLEAN_RETRY_SHAPE, feedback=feedback, invoke=invoke)


def enqueue_clean_synthesize(job_id: str, table_id: str, *, invoke: Any = None) -> None:
    _dispatch(job_id, table_id, TASK_CLEAN_SYNTHESIZE, invoke=invoke)


def enqueue_clean_retry_transform(job_id: str, table_id: str, *, feedback: str, invoke: Any = None) -> None:
    _dispatch(job_id, table_id, TASK_CLEAN_RETRY_TRANSFORM, feedback=feedback, invoke=invoke)


def _load_sample(job: dict[str, Any], table: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Headers + sample rows for the table's CURRENT region of record.

    Reads ``table["source_region"]`` (which reflects any operator-approved
    phase-1 correction — see ``extract_review._adopt_corrected_region``), not
    the original parser output, so phase 2 always cleans the region the
    operator actually approved.
    """
    from hiveflow.spreadsheet.sample import extract_table_sample
    from hiveflow.spreadsheet_lab import store

    region = table.get("source_region") or {}
    headers = [str(h) for h in (region.get("headers") or []) if str(h).strip()]
    filename = str(job.get("filename") or "workbook.xlsx")
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(store.load_upload_bytes(job))
        sample = extract_table_sample(
            local_path,
            sheet=str(table.get("sheet") or ""),
            data_start_row=int(region.get("data_start_row") or 0),
            data_end_row=int(region.get("data_end_row") or 0),
            min_col=int(region.get("min_col") or 1),
            max_col=int(region.get("max_col") or 1),
            headers=headers,
            header_col_offsets=list(region.get("header_col_offsets") or []),
        )
    rows = list(sample.get("rows") or [])
    return headers, rows


def run_table_task(payload: dict[str, Any]) -> dict[str, Any]:
    """The actual work — run inline (local/dev) or from the async self-invoke."""
    from hiveflow.spreadsheet_lab import clean_agent, extract_agent, store

    job_id = str(payload.get("job_id") or "")
    table_id = str(payload.get("table_id") or "")
    task = str(payload.get("task") or "")
    invoke = payload.get("invoke")

    job = store.load_job(job_id)
    table = store.load_table(job_id, table_id)
    if not job or not table:
        raise ValueError(f"Unknown job/table {job_id!r}/{table_id!r}")

    if task in (TASK_EXTRACT_VALIDATE, TASK_EXTRACT_RETRY):
        parse_payload = store.load_parse(job_id) or {}
        parse_table = next(
            (
                t
                for t in parse_payload.get("tables") or []
                if isinstance(t, dict) and str(t.get("table_id")) == table_id
            ),
            table.get("source_region") or {},
        )
        with tempfile.TemporaryDirectory() as tmp:
            workbook_path: str | None
            try:
                filename = str(job.get("filename") or "workbook.xlsx")
                local_path = Path(tmp) / filename
                local_path.write_bytes(store.load_upload_bytes(job))
                workbook_path = str(local_path)
            except Exception:  # noqa: BLE001 — agent falls back to heuristic without a workbook
                workbook_path = None

            if task == TASK_EXTRACT_VALIDATE:
                proposal = extract_agent.propose_table_extraction(
                    workbook_path=workbook_path, parse_table=parse_table, invoke=invoke
                )
            else:
                feedback = str(payload.get("feedback") or "")
                proposal = extract_agent.propose_table_extraction(
                    workbook_path=workbook_path,
                    parse_table=parse_table,
                    feedback=feedback,
                    prior_proposal=table.get("extract_proposal"),
                    invoke=invoke,
                )
        table["extract_proposal"] = proposal
        table["status"] = "pending_review"
        store.save_table(table)
        return {"job_id": job_id, "table_id": table_id, "status": "ok"}

    if task in (TASK_CLEAN_PROPOSE, TASK_CLEAN_RETRY_SHAPE):
        headers, rows = _load_sample(job, table)
        if task == TASK_CLEAN_PROPOSE:
            result = clean_agent.propose_clean(headers=headers, rows=rows, table=table, invoke=invoke)
        else:
            feedback = str(payload.get("feedback") or "")
            result = clean_agent.propose_clean(
                headers=headers,
                rows=rows,
                table=table,
                feedback=feedback,
                prior_goal=table.get("clean_goal"),
                invoke=invoke,
            )
        table.update(result)
        table["status"] = "pending_shape_review"
        store.save_table(table)
        return {"job_id": job_id, "table_id": table_id, "status": "ok"}

    if task in (TASK_CLEAN_SYNTHESIZE, TASK_CLEAN_RETRY_TRANSFORM):
        headers, rows = _load_sample(job, table)
        clean_goal = table.get("clean_goal") or {}
        if task == TASK_CLEAN_SYNTHESIZE:
            result = clean_agent.synthesize_clean(
                headers=headers, rows=rows, clean_goal=clean_goal, table=table, invoke=invoke
            )
        else:
            feedback = str(payload.get("feedback") or "")
            result = clean_agent.synthesize_clean(
                headers=headers,
                rows=rows,
                clean_goal=clean_goal,
                table=table,
                feedback=feedback,
                invoke=invoke,
            )
        table.update(result)
        table["status"] = "pending_transform_review"
        store.save_table(table)
        return {"job_id": job_id, "table_id": table_id, "status": "ok"}

    raise ValueError(f"Unknown task {task!r}")
