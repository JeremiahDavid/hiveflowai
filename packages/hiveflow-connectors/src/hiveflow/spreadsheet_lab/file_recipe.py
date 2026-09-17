"""Phase-1 recipe: deterministic replay of a file's table-extraction review outcome.

Once every table detected in a file has been approved or discarded, the outcome
(region + schema per table) is compiled into a recipe keyed by a whole-file
shape signature. A future upload whose shape matches replays the recipe
directly — zero LLM calls — and skips phase-1 review entirely. This is what
satisfies "future uploads run through a specific prompt that gets the desired
output" without literally re-invoking an agent every time.
"""

from __future__ import annotations

import hashlib
from typing import Any

from hiveflow.spreadsheet.transform import compute_input_shape
from hiveflow.spreadsheet_lab import store
from hiveflow.storage.blobstore import read_json, resolve_blob_location, write_json
from hiveflow.storage.paths import spreadsheet_lab_file_recipe_key

RECIPE_KIND = "spreadsheet_lab_file_recipe"
TERMINAL_STATUSES = frozenset({"approved", "discarded"})


def compute_file_shape_hash(parse_payload: dict[str, Any]) -> str:
    """Whole-file shape signature: every table's own shape_hash, order-independent."""
    table_hashes = sorted(
        str(compute_input_shape(t).get("shape_hash") or "")
        for t in parse_payload.get("tables") or []
        if isinstance(t, dict)
    )
    content = "|".join(table_hashes)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def find_matching_file_recipe(file_shape_hash: str) -> dict[str, Any] | None:
    if not file_shape_hash:
        return None
    return read_json(resolve_blob_location(), spreadsheet_lab_file_recipe_key(file_shape_hash))


def all_tables_terminal(job_id: str) -> bool:
    tables = store.load_tables(job_id)
    if not tables:
        return False
    return all(str(t.get("status") or "") in TERMINAL_STATUSES for t in tables)


def compile_file_recipe(job_id: str) -> dict[str, Any]:
    """Compile every table's approved/discarded extraction outcome into a recipe.

    Only phase-1 (extraction) outcomes are captured here — a table's cleaning
    recipe (phase 2) is compiled separately, per table, once that table's
    cleaning is approved (see ``table_recipe.py``).
    """
    job = store.load_job(job_id)
    if not job:
        raise ValueError(f"Unknown job {job_id!r}")
    file_shape_hash = str(job.get("file_shape_hash") or "")
    if not file_shape_hash:
        raise ValueError(f"Job {job_id!r} has no file_shape_hash")

    tables = store.load_tables(job_id)
    recipe = {
        "kind": RECIPE_KIND,
        "recipe_id": file_shape_hash,
        "shape_hash": file_shape_hash,
        "version": 1,
        "source_job_id": job_id,
        "filename_hint": str(job.get("filename") or ""),
        "tables": [
            {
                "table_id": table.get("table_id"),
                "sheet": table.get("sheet"),
                "source_region": table.get("source_region"),
                "extract_proposal": table.get("extract_proposal"),
                "input_shape": table.get("input_shape"),
                "status": table.get("status"),
            }
            for table in tables
        ],
    }
    write_json(resolve_blob_location(), spreadsheet_lab_file_recipe_key(file_shape_hash), recipe)
    return recipe


def replay_file_recipe(job_id: str, recipe: dict[str, Any]) -> None:
    """Write each table doc directly from a matched recipe — zero LLM calls,
    but NOT zero human review.

    A saved recipe means the agent doesn't need to run again — it does not
    mean the operator's oversight is skipped too. A table the recipe marked
    ``approved`` lands back in ``pending_review`` with its ``extract_proposal``
    pre-filled verbatim from the recipe (so confirming it is a single click,
    no new AI call and no re-typed feedback), landing it in the same
    ``awaiting_extract_review`` flow as a fresh upload and letting
    ``extract_review._maybe_compile_recipe`` carry it into phase 2 exactly
    like any other table once approved. A table the recipe marked
    ``discarded`` replays straight to ``discarded`` — that verdict doesn't
    need re-confirming on every re-upload.

    ``input_shape`` always comes from the recipe, never recomputed from this
    upload's fresh heuristic parse: a header cell the detector can't read
    (e.g. a genuinely blank cell whose column the agent named from context,
    like "service_charge") is blank on every re-upload too, so recomputing
    would silently rename it back to a generic ``column_N`` and produce a
    different ``shape_hash`` than the one the table-cleaning recipe (phase 2)
    was saved under — defeating that recipe's lookup on every replay.
    Confirmed by a real re-upload where this caused phase 2 to require full
    review again despite an unchanged file shape.
    """
    table_ids: list[str] = []
    for entry in recipe.get("tables") or []:
        table_id = str(entry.get("table_id") or "")
        if not table_id:
            continue
        input_shape = entry.get("input_shape") or {}
        table_doc = store.new_table(
            job_id=job_id,
            table_id=table_id,
            sheet=str(entry.get("sheet") or ""),
            source_region=entry.get("source_region") or {},
            input_shape=input_shape,
        )
        table_doc["extract_proposal"] = entry.get("extract_proposal")
        recipe_status = str(entry.get("status") or "approved")
        table_doc["status"] = "discarded" if recipe_status == "discarded" else "pending_review"
        store.save_table(table_doc)
        table_ids.append(table_id)

    job = store.load_job(job_id) or {}
    job["table_ids"] = table_ids
    store.save_job(job)
