"""Phase-1 human-in-the-loop review: approve/discard/reject a table's extraction proposal.

Each table has its own independent flow — approving or discarding one table
never touches another table's state — so an operator can work through a
multi-table workbook one card at a time, and the ``feedback_history``/
``attempt_count`` on a rejected table track exactly how many rounds it took.
"""

from __future__ import annotations

from typing import Any

from hiveflow.spreadsheet.transform import compute_input_shape
from hiveflow.spreadsheet_lab import clean_review, file_recipe, store, worker


def approve_extraction(job_id: str, table_id: str, *, invoke: Any = None) -> dict[str, Any]:
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    _adopt_corrected_region(table)
    store.promote_extract_proposal(table)
    table["status"] = "approved"
    store.save_table(table)
    _maybe_compile_recipe(job_id, invoke=invoke)
    # _maybe_compile_recipe may have handed this same table straight to phase 2
    # (start_clean_phase), which mutates it independently of the local `table`
    # object above — re-load rather than return the now-stale copy.
    return store.load_table(job_id, table_id) or table


def _adopt_corrected_region(table: dict[str, Any]) -> None:
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
    if corrected.get("min_col") is not None or corrected.get("max_col") is not None:
        # A corrected boundary invalidates the original detector's column-gap
        # offsets; assume a contiguous range across the corrected boundary
        # (side-by-side/gapped tables are a heuristic-parser edge case the
        # agent isn't expected to reverse-engineer).
        region["header_col_offsets"] = (
            list(range(0, int(max_col) - int(min_col) + 1))
            if min_col is not None and max_col is not None
            else region.get("header_col_offsets")
        )
    table["source_region"] = region
    table["input_shape"] = compute_input_shape({"sheet": table.get("sheet"), "headers": region.get("headers")})


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
