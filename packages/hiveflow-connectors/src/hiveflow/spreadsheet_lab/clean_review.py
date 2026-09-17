"""Phase-2 human-in-the-loop review: approve/reject a table's cleaned shape,
then its deterministic transformation.

Mirrors the phase-1 review shape (``extract_review.py``) but for cleaning: each
table has its own independent shape-review -> transform-review flow, and
rejecting either step with feedback re-invokes the corresponding agent call
fresh (prior goal/transformation + feedback), never a persisted conversation —
the same pattern ``hiveflow.spreadsheet.synthesize`` already proves out.

Once a table's transformation is approved, it materializes immediately
(deterministic — no AI call, no async worker needed) and a table-level recipe
is saved so a future table with the same input shape skips phase 2 entirely.
"""

from __future__ import annotations

import logging
from typing import Any

from hiveflow.spreadsheet_lab import file_recipe, store, table_recipe, worker

_logger = logging.getLogger(__name__)


def start_clean_phase(job_id: str, *, invoke: Any = None) -> None:
    """Hand every phase-1-approved table off to cleaning.

    A table whose shape already has an approved cleaning recipe (matched by
    ``input_shape.shape_hash``) skips phase 2's AI calls entirely — the
    recipe's transformation is pre-filled and the table lands directly at
    the transform-review checkpoint for a one-click confirm, rather than
    going through shape-review too (there's no cached ``clean_goal`` preview
    to show, only the final transformation). A recipe means zero LLM calls,
    not zero human review — see ``_replay_table_recipe``.
    """
    for table in store.load_tables(job_id):
        if table.get("phase") != "extract" or table.get("status") != "approved":
            continue
        recipe = table_recipe.find_matching_table_recipe(table.get("input_shape") or {})
        if recipe:
            _replay_table_recipe(job_id, table, recipe)
        else:
            table["phase"] = "clean"
            table["status"] = "processing"
            store.save_table(table)
            worker.enqueue_clean_propose(job_id, table["table_id"], invoke=invoke)


def _replay_table_recipe(job_id: str, table: dict[str, Any], recipe: dict[str, Any]) -> None:
    table["phase"] = "clean"
    table["clean_shape_status"] = "approved"
    table["transformation"] = recipe.get("transformation") or {"version": 1, "steps": []}
    table["transformation_status"] = "pending_review"
    table["transformation_confidence"] = 1.0
    table["transformation_notes"] = [
        "This table's shape matched a saved cleaning recipe — the "
        "transformation below was replayed with no AI calls. Review and "
        "approve to materialize."
    ]
    table["status"] = "pending_transform_review"
    store.save_table(table)


def discard_table(job_id: str, table_id: str) -> dict[str, Any]:
    """Drop a table out of the pipeline during phase 2 review — a table that
    looked worth extracting can still turn out to be noise (a report's title
    block, a boilerplate metadata table, etc.) only once its cleaned preview
    is visible. Mirrors ``extract_review.discard_table``'s phase-1 action;
    unlike that one, this can fire from either the shape-review or
    transform-review checkpoint, so it's on the table itself rather than a
    specific stage's approve/reject pair."""
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    table["status"] = "discarded"
    store.save_table(table)
    _maybe_finish_job(job_id)
    return table


def approve_clean_shape(job_id: str, table_id: str, *, invoke: Any = None) -> dict[str, Any]:
    """Lock the cleaned shape as the final goal and synthesize deterministic steps."""
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    clean_goal = table.get("clean_goal") or {}
    if not clean_goal.get("headers"):
        # `rows` is deliberately NOT required here — an empty list is a valid,
        # well-formed result from propose_clean for a table that genuinely
        # has no data rows this run (e.g. a report's optional "request page
        # option" section left blank) — confirmed via a real table whose
        # heuristic-fallback clean_goal was `{headers: [...], rows: []}`.
        # Only a missing/absent `headers` means propose_clean never actually
        # ran (or produced a malformed result).
        raise ValueError("Clean goal is missing headers — propose_clean must run first.")
    table["clean_shape_status"] = "approved"
    table["status"] = "processing"
    store.save_table(table)
    worker.enqueue_clean_synthesize(job_id, table_id, invoke=invoke)
    return table


def reject_clean_shape(
    job_id: str, table_id: str, *, feedback: str, by: str = "", invoke: Any = None
) -> dict[str, Any]:
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    text = feedback.strip()
    if not text:
        raise ValueError("Feedback is required to retry a rejected clean shape.")
    store.append_feedback(table, text=text, by=by)
    table["status"] = "processing"
    store.save_table(table)
    worker.enqueue_clean_retry_shape(job_id, table_id, feedback=text, invoke=invoke)
    return table


def approve_transformation(job_id: str, table_id: str) -> dict[str, Any]:
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    if not (table.get("transformation") or {}).get("steps"):
        raise ValueError("No synthesized transformation to approve yet.")
    table["transformation_status"] = "approved"
    table["status"] = "approved"
    store.save_table(table)
    table_recipe.compile_table_recipe(table)
    _materialize(job_id, table)
    return store.load_table(job_id, table_id) or table


def reject_transformation(
    job_id: str, table_id: str, *, feedback: str, by: str = "", invoke: Any = None
) -> dict[str, Any]:
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    text = feedback.strip()
    if not text:
        raise ValueError("Feedback is required to retry a rejected transformation.")
    store.append_feedback(table, text=text, by=by)
    table["status"] = "processing"
    store.save_table(table)
    worker.enqueue_clean_retry_transform(job_id, table_id, feedback=text, invoke=invoke)
    return table


def _materialize(job_id: str, table: dict[str, Any]) -> None:
    """Write the approved table to silver/reference_lab/.

    Goes through ``worker.run_materialize`` rather than importing
    ``materialize_lab`` directly here — that keeps ``pyarrow`` (needed only
    for the parquet write) out of every module this one imports at load time,
    which matters because the deployed web/worker Lambda ships a *separate*,
    dedicated materialize Lambda instead of bundling pyarrow alongside
    pandas/pydantic/python-calamine (that combination blew past Lambda's
    250MB unzipped limit on the first real deploy — see worker.py).
    """
    silver_payload = worker.run_materialize(job_id, str(table.get("table_id") or ""))
    if silver_payload is not None:
        table["silver"] = silver_payload
    table["phase"] = "done"
    store.save_table(table)
    _maybe_finish_job(job_id)


def _maybe_finish_job(job_id: str) -> None:
    tables = store.load_tables(job_id)
    if not tables:
        return
    if all(t.get("phase") == "done" or t.get("status") == "discarded" for t in tables):
        job = store.load_job(job_id) or {}
        job["status"] = "ready"
        store.save_job(job)
        # Recompile the file recipe now that every table has its FINAL
        # outcome — a table discarded during phase 2 (clean_review.discard_table)
        # only gets its status flipped after the phase-1 recipe was already
        # compiled, so without this a re-upload would "forget" that discard
        # and send the table through phase 2 review all over again.
        try:
            file_recipe.compile_file_recipe(job_id)
        except ValueError:
            _logger.exception("Failed to recompile file recipe for job %r at finish", job_id)
