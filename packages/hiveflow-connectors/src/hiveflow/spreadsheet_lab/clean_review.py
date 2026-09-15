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

from typing import Any

from hiveflow.spreadsheet_lab import materialize_lab, store, table_recipe, worker


def start_clean_phase(job_id: str, *, invoke: Any = None) -> None:
    """Hand every phase-1-approved table off to cleaning.

    A table whose shape already has an approved cleaning recipe (matched by
    ``input_shape.shape_hash``) skips phase 2's review entirely — the recipe
    replays and the table materializes immediately, with zero LLM calls.
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
    table["transformation"] = recipe.get("transformation") or {"version": 1, "steps": []}
    table["transformation_status"] = "approved"
    table["status"] = "approved"
    store.save_table(table)
    _materialize(job_id, table)


def approve_clean_shape(job_id: str, table_id: str, *, invoke: Any = None) -> dict[str, Any]:
    """Lock the cleaned shape as the final goal and synthesize deterministic steps."""
    table = store.load_table(job_id, table_id)
    if not table:
        raise ValueError(f"Unknown table {table_id!r} for job {job_id!r}")
    clean_goal = table.get("clean_goal") or {}
    if not clean_goal.get("headers") or not clean_goal.get("rows"):
        raise ValueError("Clean goal is missing headers/rows — propose_clean must run first.")
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
    job = store.load_job(job_id) or {}
    upload_body = store.load_upload_bytes(job)
    result = materialize_lab.materialize_approved_table_lab(job=job, table=table, upload_body=upload_body)
    if result is not None:
        table["silver"] = materialize_lab.materialization_payload(result, materialized_at=store.now_iso())
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
