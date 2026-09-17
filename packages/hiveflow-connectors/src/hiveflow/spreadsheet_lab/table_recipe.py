"""Phase-2 recipe: deterministic replay of a table's cleaning transformation.

Keyed by the table's own input-shape hash (reusing ``hiveflow.spreadsheet.transform``'s
``shape_hash`` directly — no new hashing scheme), a table recipe IS a
``transform.py`` step spec. Replaying it means re-running ``apply_transformation``
with no LLM call on any future table (in any file) whose shape matches — the
whole point of "future uploads run the same cleanup" without re-asking a human.
"""

from __future__ import annotations

from typing import Any

from hiveflow.storage.blobstore import read_json, resolve_blob_location, write_json
from hiveflow.storage.paths import spreadsheet_engine_table_recipe_key

RECIPE_KIND = "spreadsheet_lab_table_recipe"


def find_matching_table_recipe(input_shape: dict[str, Any]) -> dict[str, Any] | None:
    shape_hash = str((input_shape or {}).get("shape_hash") or "")
    if not shape_hash:
        return None
    return read_json(resolve_blob_location(), spreadsheet_engine_table_recipe_key(shape_hash))


def compile_table_recipe(table: dict[str, Any]) -> dict[str, Any]:
    input_shape = table.get("input_shape") or {}
    shape_hash = str(input_shape.get("shape_hash") or "")
    if not shape_hash:
        raise ValueError("Table has no input_shape.shape_hash to key a recipe on")
    recipe = {
        "kind": RECIPE_KIND,
        "recipe_id": shape_hash,
        "input_shape_hash": shape_hash,
        "version": 1,
        "source_job_id": table.get("job_id"),
        "source_table_id": table.get("table_id"),
        "transformation": table.get("transformation") or {"version": 1, "steps": []},
    }
    write_json(resolve_blob_location(), spreadsheet_engine_table_recipe_key(shape_hash), recipe)
    return recipe
