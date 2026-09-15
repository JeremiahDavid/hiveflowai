"""Phase 3a: materialize an approved, cleaned table into silver/reference_lab/.

Reuses two of the production Spreadsheet Engine's building blocks directly:
``hiveflow.spreadsheet.preview.extract_table_preview`` (public) for full-workbook
row extraction, and ``hiveflow.spreadsheet.transform.apply_transformation``
(public) for cast-safe row transformation — the same functions
``hiveflow.spreadsheet.materialize`` itself is built on. This module does NOT
reuse ``materialize._run_transformation_over_full_data`` itself, because that
helper re-derives a table's region by looking it up in ``parse_payload`` by
``table_id`` — which would silently ignore an operator-approved phase-1 region
correction (see ``extract_review._adopt_corrected_region``) and clean/materialize
the ORIGINAL wrong region instead. This version reads the region straight off
the table doc's own ``source_region``, which always reflects the approved
correction.

The destination differs from production too: ``silver/reference_lab/{entity}``
(Spreadsheet Lab's own prefix), never ``silver/reference/{entity}`` (the
production engine's), so lab test data can never land next to, or overwrite,
real reference entities.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet.materialize import SilverMaterialization, _normalize_entity_name, _rows_to_dicts, materialization_payload
from hiveflow.spreadsheet.preview import extract_table_preview
from hiveflow.spreadsheet.transform import apply_transformation, build_output_shape
from hiveflow.storage.blobstore import resolve_blob_location
from hiveflow.storage.column_names import normalize_silver_rows
from hiveflow.storage.parquet import write_parquet_local, write_parquet_s3
from hiveflow.storage.paths import (
    SPREADSHEET_LAB_REFERENCE_SOURCE,
    prefix_path,
    spreadsheet_lab_reference_silver_entity_parquet_key,
)

__all__ = ["materialize_approved_table_lab", "materialization_payload"]


def _write_reference_silver_parquet(
    entity_name: str,
    rows: list[dict[str, Any]],
    *,
    issues: list[str] | None = None,
) -> SilverMaterialization:
    entity = _normalize_entity_name(entity_name)
    parquet_key = spreadsheet_lab_reference_silver_entity_parquet_key(entity)
    normalized_rows = normalize_silver_rows(rows)
    loc = resolve_blob_location()
    if loc.bucket:

        class _Dest:
            s3_bucket = loc.bucket
            s3_prefix = ""
            data_dir = loc.data_dir

        location = write_parquet_s3(_Dest(), parquet_key, normalized_rows)
    else:
        out_dir = prefix_path(loc.data_dir, parquet_key).parent
        location = write_parquet_local(out_dir, "data.parquet", normalized_rows)
    return SilverMaterialization(
        source=SPREADSHEET_LAB_REFERENCE_SOURCE,
        entity=entity,
        parquet_key=parquet_key,
        location=location,
        row_count=len(normalized_rows),
        issues=list(issues or []),
    )


def materialize_approved_table_lab(
    *,
    job: dict[str, Any],
    table: dict[str, Any],
    upload_body: bytes,
) -> SilverMaterialization | None:
    """Extract every workbook row for the table's approved region, apply its
    approved transformation, and write silver/reference_lab/{entity} parquet."""
    region = table.get("source_region") or {}
    raw_headers = [str(name) for name in (region.get("headers") or []) if str(name).strip()]
    if not raw_headers:
        return None

    transformation = dict(table.get("transformation") or {})
    if not transformation.get("output_shape"):
        transformation["output_shape"] = build_output_shape(table)

    filename = str(job.get("filename") or "workbook.xlsx")
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / filename
        local_path.write_bytes(upload_body)
        extracted = extract_table_preview(
            local_path,
            sheet=str(table.get("sheet") or ""),
            data_start_row=int(region.get("data_start_row") or 0),
            data_end_row=int(region.get("data_end_row") or 0),
            min_col=int(region.get("min_col") or 1),
            max_col=int(region.get("max_col") or 1),
            headers=raw_headers,
            header_col_offsets=list(region.get("header_col_offsets") or []),
            max_rows=None,
        )

    rows = list(extracted.get("rows") or [])
    issues: list[str] = []
    out_rows, out_headers = apply_transformation(rows, raw_headers, transformation, issues=issues)
    if not out_headers:
        return None

    entity_name = str(table.get("entity_name") or table.get("table_id") or "")
    return _write_reference_silver_parquet(
        entity_name, _rows_to_dicts(out_headers, out_rows), issues=issues
    )
