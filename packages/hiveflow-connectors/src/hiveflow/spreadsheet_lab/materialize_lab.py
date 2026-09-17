"""Materialize an approved, cleaned table into silver/reference/{entity}.

Reuses two of the (retired) production Spreadsheet Engine's low-level building
blocks directly: ``hiveflow.spreadsheet.preview.extract_table_preview``
(public) for full-workbook row extraction, and
``hiveflow.spreadsheet.transform.apply_transformation`` (public) for
cast-safe row transformation — the same functions
``hiveflow.spreadsheet.materialize`` itself is built on. This module does NOT
reuse ``materialize._run_transformation_over_full_data`` itself, because that
helper re-derives a table's region by looking it up in ``parse_payload`` by
``table_id`` — which would silently ignore an operator-approved phase-1 region
correction (see ``extract_review._adopt_corrected_region``) and clean/materialize
the ORIGINAL wrong region instead. This version reads the region straight off
the table doc's own ``source_region``, which always reflects the approved
correction.

Writes to the same ``silver/reference/{entity}`` location the old engine used —
this module is now the only Spreadsheet Engine implementation.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet.materialize import SilverMaterialization, _normalize_entity_name, _rows_to_dicts, materialization_payload
from hiveflow.spreadsheet.preview import extract_table_preview
from hiveflow.spreadsheet.transform import apply_transformation, build_output_shape
from hiveflow.storage.blobstore import resolve_blob_location
from hiveflow.storage.column_names import normalize_silver_rows
from hiveflow.storage.parquet import write_parquet_local, write_parquet_s3
from hiveflow.storage.paths import (
    SPREADSHEET_REFERENCE_SOURCE,
    prefix_path,
    spreadsheet_reference_silver_entity_parquet_key,
)

__all__ = [
    "materialize_approved_table_lab",
    "materialization_payload",
    "lab_materialization_payload",
    "lambda_handler",
]

PREVIEW_ROW_LIMIT = 20


@dataclass
class LabMaterialization:
    """Wraps the shared ``SilverMaterialization`` with a small row preview so
    an operator can confirm the materialized output in the review UI without
    needing pyarrow (or S3/Athena access) in the web Lambda — see
    ``lab_materialization_payload``."""

    result: SilverMaterialization
    preview_headers: list[str]
    preview_rows: list[list[Any]]


def lab_materialization_payload(lab_result: LabMaterialization, *, materialized_at: str) -> dict[str, Any]:
    payload = materialization_payload(lab_result.result, materialized_at=materialized_at)
    payload["silver_preview_headers"] = lab_result.preview_headers
    payload["silver_preview_rows"] = lab_result.preview_rows
    return payload


def _coerce_uniform_column_types(
    rows: list[dict[str, Any]], *, issues: list[str]
) -> list[dict[str, Any]]:
    """Stringify any column whose values span more than one Python type.

    pyarrow's ``Table.from_pylist`` hard-crashes (``ArrowTypeError``) on a
    column mixing e.g. ``str`` and ``int`` — confirmed by a real approved
    table whose synthesized transform left a "value" column with 9 text
    rows and one raw numeric cell (no ``cast`` step ever ran over it, since
    the transform never explicitly typed that column). An operator's
    approval should never be lost to that: a table this messy is exactly
    the kind of raw, uncleaned spreadsheet data this tool exists to handle,
    so widen the column to text rather than crashing. Not fixed upstream in
    ``hiveflow.storage.parquet`` (shared by every connector's ingest writes)
    — that blast radius is out of scope for a Spreadsheet Lab data-quality
    guard.
    """
    if not rows:
        return rows
    columns: dict[str, set[type]] = {}
    for row in rows:
        for key, value in row.items():
            if value is None:
                continue
            columns.setdefault(key, set()).add(type(value))
    mixed_columns = {key for key, types in columns.items() if len(types) > 1}
    if not mixed_columns:
        return rows
    issues.append(
        "Column(s) "
        + ", ".join(sorted(mixed_columns))
        + " had mixed data types in the raw sheet; converted to text so the row wasn't dropped."
    )
    coerced: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        for key in mixed_columns:
            if key in new_row and new_row[key] is not None:
                new_row[key] = str(new_row[key])
        coerced.append(new_row)
    return coerced


def _write_reference_silver_parquet(
    entity_name: str,
    rows: list[dict[str, Any]],
    *,
    issues: list[str] | None = None,
) -> SilverMaterialization:
    entity = _normalize_entity_name(entity_name)
    parquet_key = spreadsheet_reference_silver_entity_parquet_key(entity)
    issues = issues if issues is not None else []
    normalized_rows = _coerce_uniform_column_types(normalize_silver_rows(rows), issues=issues)
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
        source=SPREADSHEET_REFERENCE_SOURCE,
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
) -> LabMaterialization | None:
    """Extract every workbook row for the table's approved region, apply its
    approved transformation, and write silver/reference/{entity} parquet."""
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
    result = _write_reference_silver_parquet(
        entity_name, _rows_to_dicts(out_headers, out_rows), issues=issues
    )
    return LabMaterialization(
        result=result,
        preview_headers=list(out_headers),
        preview_rows=[list(row) for row in out_rows[:PREVIEW_ROW_LIMIT]],
    )


def lambda_handler(event: dict[str, Any] | None, _context: Any) -> dict[str, Any]:
    """Entry point for the dedicated materialize Lambda (see worker.run_materialize).

    Takes ``{job_id, table_id, company, bucket}`` — re-reads the job/table/
    workbook itself from S3 (binding to the tenant carried in ``company``/
    ``bucket``, see ``worker._worker_tenant_scope``) rather than taking them
    as payload, since this Lambda is invoked synchronously and a real
    workbook can easily exceed the 6MB payload cap. This is the only Lambda
    in Spreadsheet Lab that needs pyarrow installed.
    """
    from hiveflow.spreadsheet_lab import store
    from hiveflow.spreadsheet_lab.worker import _worker_tenant_scope

    payload = event or {}
    with _worker_tenant_scope(payload):
        job_id = str(payload.get("job_id") or "")
        table_id = str(payload.get("table_id") or "")
        job = store.load_job(job_id)
        table = store.load_table(job_id, table_id)
        if not job or not table:
            raise ValueError(f"Unknown job/table {job_id!r}/{table_id!r}")

        upload_body = store.load_upload_bytes(job)
        result = materialize_approved_table_lab(job=job, table=table, upload_body=upload_body)
        silver = lab_materialization_payload(result, materialized_at=store.now_iso()) if result else None
        return {"silver": silver}
