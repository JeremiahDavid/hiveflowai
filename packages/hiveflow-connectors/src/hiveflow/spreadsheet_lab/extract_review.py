"""Phase-1 human-in-the-loop review: approve/discard/reject a table's extraction proposal.

Each table has its own independent flow — approving or discarding one table
never touches another table's state — so an operator can work through a
multi-table workbook one card at a time, and the ``feedback_history``/
``attempt_count`` on a rejected table track exactly how many rounds it took.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet import parser as _parser
from hiveflow.spreadsheet.transform import compute_input_shape
from hiveflow.spreadsheet_lab import clean_review, file_recipe, store, worker

_logger = logging.getLogger(__name__)


def approve_extraction(job_id: str, table_id: str, *, invoke: Any = None) -> dict[str, Any]:
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    job = store.load_job(job_id)
    _adopt_corrected_region(table, job=job)
    store.promote_extract_proposal(table)
    table["status"] = "approved"
    store.save_table(table)
    _maybe_compile_recipe(job_id, invoke=invoke)
    # _maybe_compile_recipe may have handed this same table straight to phase 2
    # (start_clean_phase), which mutates it independently of the local `table`
    # object above — re-load rather than return the now-stale copy.
    return store.load_table(job_id, table_id) or table


def _adopt_corrected_region(table: dict[str, Any], *, job: dict[str, Any] | None) -> None:
    """Fold an agent-proposed region correction into the table's region of record.

    Without this, an operator approving a corrected proposal would silently
    keep sampling/materializing from the ORIGINAL (wrong) detected region in
    every later phase — the correction would only ever have existed in the
    review UI, never actually taken effect.
    """
    proposal = table.get("extract_proposal") or {}
    corrected = proposal.get("corrected_region")
    if not corrected:
        return
    region = dict(table.get("source_region") or {})
    for key in ("header_row", "data_start_row", "data_end_row", "min_col", "max_col", "headers"):
        if corrected.get(key) is not None:
            region[key] = corrected[key]
    min_col, max_col = region.get("min_col"), region.get("max_col")
    if (corrected.get("min_col") is not None or corrected.get("max_col") is not None) and job is not None:
        # A corrected boundary can drop a blank/merged spacer column from the
        # middle of the range (e.g. min_col=A, max_col=E, but column B is a
        # blank spacer and `headers` only lists the 4 real columns) — in that
        # case a dense 0..N offset range misaligns every header after the gap
        # by one column, silently reading the wrong data into every field
        # from that point on (confirmed by a real approved table whose
        # "service_charge" column ended up reading a blank spacer column,
        # zeroing every row via a downstream not-null filter). Re-read the
        # real sheet with the same region-building logic the blind-scan path
        # already uses to find the TRUE non-blank column positions, and keep
        # the agent's semantic header names layered on top of those
        # positions rather than trusting a guessed contiguous range.
        rebuilt = _rebuild_region_offsets(job, region, sheet=str(table.get("sheet") or ""))
        if rebuilt is not None:
            region["header_col_offsets"] = rebuilt["header_col_offsets"]
            region["data_start_row"] = rebuilt["data_start_row"]
            region["data_end_row"] = rebuilt["data_end_row"]
            if len(region.get("headers") or []) != len(rebuilt["header_col_offsets"]):
                # Agent's header count doesn't match the real non-blank column
                # count — trust the structural read over the semantic names.
                region["headers"] = rebuilt["headers"]
        else:
            # Workbook unavailable/unreadable — fall back to the previous
            # best-effort guess rather than leaving stale offsets in place.
            region["header_col_offsets"] = (
                list(range(0, int(max_col) - int(min_col) + 1))
                if min_col is not None and max_col is not None
                else region.get("header_col_offsets")
            )
    table["source_region"] = region
    table["input_shape"] = compute_input_shape({"sheet": table.get("sheet"), "headers": region.get("headers")})


def _rebuild_region_offsets(
    job: dict[str, Any], region: dict[str, Any], *, sheet: str
) -> dict[str, Any] | None:
    """Re-read the real workbook to find the true non-blank column positions
    within the corrected boundaries. Returns ``None`` on any failure (missing
    upload, unknown sheet, degenerate region) so the caller can fall back."""
    try:
        from openpyxl import load_workbook

        sheet_name = sheet
        header_row = int(region["header_row"])
        data_end_row = int(region["data_end_row"])
        min_col = int(region["min_col"])
        max_col = int(region["max_col"])
        with tempfile.TemporaryDirectory() as tmp:
            filename = str(job.get("filename") or "workbook.xlsx")
            local_path = Path(tmp) / filename
            local_path.write_bytes(store.load_upload_bytes(job))
            workbook = load_workbook(local_path, data_only=True)
            try:
                sheet = workbook[sheet_name]
                rebuilt = _parser._build_region(
                    sheet,
                    header_row=header_row,
                    data_end_row=data_end_row,
                    min_col=min_col,
                    max_col=max_col,
                    require_header_heuristic=False,
                    min_data_rows=0,
                )
            finally:
                workbook.close()
        return rebuilt
    except Exception:  # noqa: BLE001 — caller falls back on None
        _logger.exception("Failed to re-read workbook to rebuild corrected region offsets")
        return None


def discard_table(job_id: str, table_id: str, *, invoke: Any = None) -> dict[str, Any]:
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    table["status"] = "discarded"
    store.save_table(table)
    _maybe_compile_recipe(job_id, invoke=invoke)
    return table


def reject_extraction(
    job_id: str, table_id: str, *, feedback: str, by: str = "", invoke: Any = None
) -> dict[str, Any]:
    """Reject a table's proposed region/shape with feedback and retry it.

    Feedback is required — a reject with no feedback has nothing for the agent
    to act on differently next time.
    """
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    text = feedback.strip()
    if not text:
        raise ValueError("Feedback is required to retry a rejected table.")
    store.append_feedback(table, text=text, by=by)
    table["status"] = "rejected_retry"
    store.save_table(table)
    worker.enqueue_extract_retry(job_id, table_id, feedback=text, invoke=invoke)
    return table


def _maybe_compile_recipe(job_id: str, *, invoke: Any = None) -> None:
    """Compile the file-level recipe once every table has a terminal status,
    then hand approved tables off to phase 2 (cleaning)."""
    if not file_recipe.all_tables_terminal(job_id):
        return
    file_recipe.compile_file_recipe(job_id)
    job = store.load_job(job_id) or {}
    job["status"] = "awaiting_clean_review"
    store.save_job(job)
    clean_review.start_clean_phase(job_id, invoke=invoke)
