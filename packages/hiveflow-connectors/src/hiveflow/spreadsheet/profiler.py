"""Column profiling for spreadsheet table candidates."""

from __future__ import annotations

from typing import Any

from hiveflow.profiling import profile_column

PROFILE_KIND = "spreadsheet_engine_profile"


def _column_values(table: dict[str, Any], col_index: int) -> list[Any]:
    values: list[Any] = []
    for row in table.get("sample_rows") or []:
        if not isinstance(row, list) or col_index >= len(row):
            continue
        values.append(row[col_index])
    return values


def profile_table(table: dict[str, Any]) -> dict[str, Any]:
    headers = list(table.get("headers") or [])
    row_count = int(table.get("row_count") or 0)
    columns: list[dict[str, Any]] = [
        profile_column(header, _column_values(table, idx)) for idx, header in enumerate(headers)
    ]
    key_candidates = [col["name"] for col in columns if col.get("likely_key")]
    return {
        "table_id": table.get("table_id"),
        "sheet": table.get("sheet"),
        "row_count": row_count,
        "column_count": len(columns),
        "columns": columns,
        "key_candidates": key_candidates,
    }


def profile_tables(parse_payload: dict[str, Any]) -> dict[str, Any]:
    tables = parse_payload.get("tables") or []
    profiles = [profile_table(table) for table in tables if isinstance(table, dict)]
    return {
        "kind": PROFILE_KIND,
        "filename": parse_payload.get("filename"),
        "table_count": len(profiles),
        "tables": profiles,
        "parse": {
            "sheet_count": parse_payload.get("sheet_count"),
            "table_count": parse_payload.get("table_count"),
        },
    }
