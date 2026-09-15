"""Materialize approved Spreadsheet Engine tables into silver/reference parquet."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hiveflow.spreadsheet.preview import extract_table_preview
from hiveflow.spreadsheet.transform import apply_transformation, build_output_shape
from hiveflow.storage.column_names import normalize_silver_rows
from hiveflow.storage.parquet import write_parquet_local, write_parquet_s3
from hiveflow.storage.paths import (
    SPREADSHEET_REFERENCE_SOURCE,
    prefix_path,
    spreadsheet_reference_silver_entity_parquet_key,
)


@dataclass(frozen=True)
class SilverMaterialization:
    source: str
    entity: str
    parquet_key: str
    location: str
    row_count: int
    issues: list[str]


def _bucket() -> str:
    raw = os.getenv("HIVEFLOW_S3_BUCKET", "").strip()
    if not raw:
        return ""
    # See hiveflow.spreadsheet.jobs._bucket for why this guard exists: a
    # leftover HIVEFLOW_S3_BUCKET in the ambient shell must never let a test
    # silently write real materialized silver data to production S3, and
    # PYTEST_CURRENT_TEST (set by pytest for every running test) is immune to
    # conftest.py discovery/rootdir quirks that an env-clearing fixture is not.
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("HIVEFLOW_ALLOW_TEST_S3"):
        return ""
    return raw


def _data_dir() -> Path:
    return Path(os.getenv("HIVEFLOW_DATA_DIR", "data")).resolve()


def _normalize_entity_name(entity_name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(entity_name or "").strip().lower()).strip("_")
    return slug or "table"


def _rows_to_dicts(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {}
        for index, header in enumerate(headers):
            key = str(header or "").strip()
            if not key:
                continue
            record[key] = row[index] if index < len(row) else None
        out.append(record)
    return out


def _write_reference_silver_parquet(
    entity_name: str,
    rows: list[dict[str, Any]],
    *,
    issues: list[str] | None = None,
) -> SilverMaterialization:
    entity = _normalize_entity_name(entity_name)
    parquet_key = spreadsheet_reference_silver_entity_parquet_key(entity)
    normalized_rows = normalize_silver_rows(rows)
    bucket = _bucket()
    if bucket:

        class _Dest:
            s3_bucket = bucket
            s3_prefix = ""
            data_dir = _data_dir()

        location = write_parquet_s3(_Dest(), parquet_key, normalized_rows)
    else:
        out_dir = prefix_path(_data_dir(), parquet_key).parent
        location = write_parquet_local(out_dir, "data.parquet", normalized_rows)
    return SilverMaterialization(
        source=SPREADSHEET_REFERENCE_SOURCE,
        entity=entity,
        parquet_key=parquet_key,
        location=location,
        row_count=len(normalized_rows),
        issues=list(issues or []),
    )


def _run_transformation_over_full_data(
    *,
    job: dict[str, Any],
    table: dict[str, Any],
    parse_payload: dict[str, Any],
    upload_body: bytes,
) -> tuple[list[list[Any]], list[str], list[str]] | None:
    """Extract every workbook row for the table and apply its transformation.

    Shared by materialize (which writes the result) and the pre-approval dry
    run (which only wants to know whether it would succeed and what it would
    warn about) — both need to run against the FULL row set, not a sample,
    since a bad value anywhere in the sheet can only surface here.
    """
    table_id = str(table.get("table_id") or "")
    parse_table = None
    for item in parse_payload.get("tables") or []:
        if isinstance(item, dict) and str(item.get("table_id") or "") == table_id:
            parse_table = item
            break
    if not parse_table:
        return None

    raw_headers = [str(name) for name in (parse_table.get("headers") or []) if str(name).strip()]
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
            sheet=str(parse_table.get("sheet") or ""),
            data_start_row=int(parse_table.get("data_start_row") or 0),
            data_end_row=int(parse_table.get("data_end_row") or 0),
            min_col=int(parse_table.get("min_col") or 1),
            max_col=int(parse_table.get("max_col") or 1),
            headers=raw_headers,
            header_col_offsets=list(parse_table.get("header_col_offsets") or []),
            max_rows=None,
        )

    rows = list(extracted.get("rows") or [])
    issues: list[str] = []
    out_rows, out_headers = apply_transformation(rows, raw_headers, transformation, issues=issues)
    return out_rows, out_headers, issues


def test_transformation_against_full_data(
    *,
    job: dict[str, Any],
    table: dict[str, Any],
    parse_payload: dict[str, Any],
    upload_body: bytes,
) -> dict[str, Any]:
    """Dry-run the table's transformation over the full sheet before it is proposed for approval.

    Returns row/issue counts so the caller can warn the operator up front —
    this is what lets approval be a no-surprises confirmation instead of the
    first time the transformation ever sees the whole dataset.
    """
    result = _run_transformation_over_full_data(
        job=job, table=table, parse_payload=parse_payload, upload_body=upload_body
    )
    if result is None:
        return {"tested": False, "row_count": 0, "issues": []}
    out_rows, out_headers, issues = result
    return {
        "tested": True,
        "row_count": len(out_rows),
        "output_headers": out_headers,
        "issues": issues,
    }


def materialize_approved_table(
    *,
    job: dict[str, Any],
    table: dict[str, Any],
    parse_payload: dict[str, Any],
    upload_body: bytes,
) -> SilverMaterialization | None:
    """Extract workbook rows, apply the approved transformation, and write silver/reference parquet."""
    table_id = str(table.get("table_id") or "")
    result = _run_transformation_over_full_data(
        job=job, table=table, parse_payload=parse_payload, upload_body=upload_body
    )
    if result is None:
        return None
    out_rows, out_headers, issues = result
    if not out_headers:
        return None

    entity_name = str(table.get("entity_name") or table_id)
    return _write_reference_silver_parquet(
        entity_name, _rows_to_dicts(out_headers, out_rows), issues=issues
    )


def materialization_payload(result: SilverMaterialization, *, materialized_at: str) -> dict[str, Any]:
    return {
        "silver_source": result.source,
        "silver_entity": result.entity,
        "silver_parquet_key": result.parquet_key,
        "silver_parquet_location": result.location,
        "silver_row_count": result.row_count,
        "silver_materialized_at": materialized_at,
        "silver_cast_issues": list(result.issues),
    }
