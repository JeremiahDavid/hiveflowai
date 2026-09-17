"""Spreadsheet Lab intake: upload, deterministic parse, and file-recipe match dispatch."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet import parser as _parser
from hiveflow.spreadsheet.parser import parse_workbook
from hiveflow.spreadsheet.transform import compute_input_shape
from hiveflow.spreadsheet_lab import extract_agent, file_recipe, store, worker

_logger = logging.getLogger(__name__)


def create_job(*, filename: str, username: str = "", company: str = "") -> dict[str, Any]:
    return store.create_job(filename=filename, username=username, company=company)


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
        if not parse_payload.get("tables"):
            # The deterministic heuristic found zero candidate regions —
            # real business-report exports (title rows, a parameters block,
            # a footnote off in some far column) routinely defeat its
            # header-detection heuristic even though there's a perfectly
            # good table on the sheet. Give the agent a shot at finding one
            # itself before giving up (confirmed necessary by a real upload
            # that heuristically parsed to zero tables despite an obvious
            # table starting mid-sheet).
            parse_payload = _blind_scan_tables(parse_payload, local_path, invoke=invoke)
    store.save_parse(job_id, parse_payload)

    file_shape_hash = file_recipe.compute_file_shape_hash(parse_payload)
    job = store.load_job(job_id) or job
    job["file_shape_hash"] = file_shape_hash
    store.save_job(job)

    matching_recipe = None if force else file_recipe.find_matching_file_recipe(file_shape_hash)
    if matching_recipe:
        # Replaying skips the AI calls, not the operator's review — every
        # previously-approved table lands back in pending_review with its
        # prior proposal pre-filled (a one-click confirm, no new AI call);
        # a previously-discarded table replays straight to discarded (that
        # verdict doesn't need re-confirming). From here this is exactly the
        # same awaiting_extract_review flow as a fresh upload —
        # extract_review._maybe_compile_recipe carries approved tables into
        # phase 2 once every table in the file is terminal, same as always.
        file_recipe.replay_file_recipe(job_id, matching_recipe)
        job = store.load_job(job_id) or job
        job["matched_file_recipe_id"] = str(matching_recipe.get("recipe_id") or "")
        job["auto_replayed"] = True
        replayed_tables = store.load_tables(job_id)
        if replayed_tables and all(
            str(t.get("status") or "") == "discarded" for t in replayed_tables
        ):
            # Every table in this file was previously discarded — nothing is
            # left to review, so the job is already finished. Without this,
            # it would sit at awaiting_extract_review forever: nothing here
            # goes through approve_extraction/discard_table (which is what
            # normally notices a job is fully terminal), since replay writes
            # already-discarded tables directly.
            job["status"] = "ready"
        else:
            job["status"] = "awaiting_extract_review"
        return store.save_job(job)

    if not parse_payload.get("tables"):
        job["status"] = "no_tables_found"
        job["error"] = (
            "No tables were detected in this workbook, even after asking the "
            "extraction agent to scan each sheet directly. Sheets checked: "
            f"{', '.join(str(s) for s in parse_payload.get('sheet_names') or [])}."
        )
        job["table_ids"] = []
        return store.save_job(job)

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


def _blind_scan_tables(
    parse_payload: dict[str, Any], workbook_path: Path, *, invoke: Any
) -> dict[str, Any]:
    """Ask the extraction agent to find a table on each sheet from scratch,
    for the case where the deterministic heuristic found nothing anywhere in
    the workbook. Returns ``parse_payload`` unchanged if the agent finds
    nothing either (``invoke=False``, no Bedrock access, or a genuinely
    tableless workbook) — callers treat an unchanged (still-empty) payload as
    "no tables found" and stop there, same as before this fallback existed.
    """
    sheet_names = parse_payload.get("selected_sheets") or parse_payload.get("sheet_names") or []
    if not sheet_names:
        return parse_payload

    from openpyxl import load_workbook

    workbook = load_workbook(workbook_path, data_only=True)
    try:
        tables: list[dict[str, Any]] = []
        table_counts = {name: 0 for name in parse_payload.get("sheet_names") or []}
        for sheet_name in sheet_names:
            proposal = extract_agent.propose_table_extraction_blind(
                workbook_path=str(workbook_path), sheet=sheet_name, invoke=invoke
            )
            region_spec = (proposal or {}).get("corrected_region")
            if not region_spec:
                continue
            try:
                region = _parser._build_region(
                    workbook[sheet_name],
                    header_row=int(region_spec["header_row"]),
                    data_end_row=int(region_spec["data_end_row"]),
                    min_col=int(region_spec["min_col"]),
                    max_col=int(region_spec["max_col"]),
                    require_header_heuristic=False,
                    min_data_rows=0,
                )
            except (KeyError, TypeError, ValueError):
                _logger.exception(
                    "Failed to build a region from the blind-scan proposal for sheet %r: %r",
                    sheet_name,
                    region_spec,
                )
                continue
            if region is None:
                continue
            table_id = f"t{len(tables)}"
            region["region_kind"] = "agent_blind_scan"
            tables.append({"table_id": table_id, **region})
            table_counts[sheet_name] = table_counts.get(sheet_name, 0) + 1
    finally:
        workbook.close()

    if not tables:
        return parse_payload

    parse_payload = dict(parse_payload)
    parse_payload["tables"] = tables
    parse_payload["table_count"] = len(tables)
    parse_payload["sheets"] = [
        {"name": name, "table_count": int(table_counts.get(name, 0))}
        for name in parse_payload.get("sheet_names") or []
    ]
    return parse_payload


def force_rerun(job_id: str, *, invoke: Any = None) -> dict[str, Any]:
    """Ignore any matched file recipe and go through full phase-1 review again."""
    return run_parse(job_id, force=True, invoke=invoke)
