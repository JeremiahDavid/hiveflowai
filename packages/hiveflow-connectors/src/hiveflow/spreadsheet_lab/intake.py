"""Spreadsheet Lab intake: upload, deterministic parse, and file-recipe match dispatch."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet.parser import parse_workbook
from hiveflow.spreadsheet.transform import compute_input_shape
from hiveflow.spreadsheet_lab import clean_review, file_recipe, store, worker


def create_job(*, filename: str, username: str = "") -> dict[str, Any]:
    return store.create_job(filename=filename, username=username)


def store_upload(job_id: str, *, filename: str, body: bytes) -> str:
    return store.store_upload(job_id, filename=filename, body=body)


def run_parse(job_id: str, *, force: bool = False, invoke: Any = None) -> dict[str, Any]:
    """Parse the uploaded workbook, then either auto-replay a matching file
    recipe (zero LLM calls, unless ``force``) or kick off phase-1 review for
    every detected table.
    """
    job = store.load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    job["status"] = "parsing"
    store.save_job(job)

    filename = str(job.get("filename") or "workbook.xlsx")
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(store.load_upload_bytes(job))
        parse_payload = parse_workbook(local_path, filename=filename)
    store.save_parse(job_id, parse_payload)

    file_shape_hash = file_recipe.compute_file_shape_hash(parse_payload)
    job = store.load_job(job_id) or job
    job["file_shape_hash"] = file_shape_hash
    store.save_job(job)

    matching_recipe = None if force else file_recipe.find_matching_file_recipe(file_shape_hash)
    if matching_recipe:
        file_recipe.replay_file_recipe(job_id, parse_payload, matching_recipe)
        job = store.load_job(job_id) or job
        job["status"] = "awaiting_clean_review"
        job["matched_file_recipe_id"] = str(matching_recipe.get("recipe_id") or "")
        job["auto_replayed"] = True
        store.save_job(job)
        clean_review.start_clean_phase(job_id, invoke=invoke)
        # start_clean_phase (and everything it triggers — table-recipe replay,
        # materialize, _maybe_finish_job) mutates the job doc independently of
        # the local `job` object above; re-load rather than return the stale copy.
        return store.load_job(job_id) or job

    job["status"] = "extracting"
    job["matched_file_recipe_id"] = ""
    job["auto_replayed"] = False
    store.save_job(job)

    table_ids: list[str] = []
    for table in parse_payload.get("tables") or []:
        table_id = str(table.get("table_id") or "")
        if not table_id:
            continue
        input_shape = compute_input_shape(table)
        table_doc = store.new_table(
            job_id=job_id,
            table_id=table_id,
            sheet=str(table.get("sheet") or ""),
            source_region={
                "header_row": table.get("header_row"),
                "data_start_row": table.get("data_start_row"),
                "data_end_row": table.get("data_end_row"),
                "min_col": table.get("min_col"),
                "max_col": table.get("max_col"),
                "headers": table.get("headers"),
                "header_col_offsets": table.get("header_col_offsets"),
            },
            input_shape=input_shape,
        )
        store.save_table(table_doc)
        table_ids.append(table_id)
        worker.enqueue_extract_validate(job_id, table_id, invoke=invoke)

    job = store.load_job(job_id) or job
    job["table_ids"] = table_ids
    job["status"] = "awaiting_extract_review"
    return store.save_job(job)


def force_rerun(job_id: str, *, invoke: Any = None) -> dict[str, Any]:
    """Ignore any matched file recipe and go through full phase-1 review again."""
    return run_parse(job_id, force=True, invoke=invoke)
