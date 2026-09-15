"""Transformation spec, shape hashing, and row-level apply for Spreadsheet Engine."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

_TRANSFORM_VERSION = 1
_NULL_SENTINEL = object()

_COL_REF = re.compile(r"^[a-z][a-z0-9_]*$")
_STRING_LITERAL = re.compile(r"^'([^']*)'$|^\"([^\"]*)\"$")
_COMPARE = re.compile(r"^(.*?)\s*(!=|==|=)\s*(.*)$")
_NOT_IN = re.compile(r"^([a-z][a-z0-9_]*)\s+not\s+in\s*\((.*)\)\s*$", re.IGNORECASE)


def slugify_filename(filename: str) -> str:
    """Normalize a workbook filename to a stable slug (without extension)."""
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", base.strip()).strip("_").lower()
    return slug or "workbook"


def normalize_header_name(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(text or "").strip()).strip("_").lower()
    return slug or "column"


def compute_input_shape(parse_table: dict[str, Any]) -> dict[str, Any]:
    """Build input shape dict + hash from a parse-table region."""
    headers = [str(h) for h in (parse_table.get("headers") or []) if str(h).strip()]
    sheet = str(parse_table.get("sheet") or "")
    column_count = len(headers)
    normalized = sorted({normalize_header_name(h) for h in headers})
    content = f"{sheet}|{column_count}|{'|'.join(normalized)}"
    shape_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return {
        "sheet": sheet,
        "headers": headers,
        "column_count": column_count,
        "shape_hash": shape_hash,
        "headers_normalized": normalized,
    }


def build_output_shape(table: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_name": str(table.get("entity_name") or ""),
        "grain": str(table.get("grain") or ""),
        "schema": list(table.get("schema") or []),
    }


def empty_transformation(
    *,
    input_shape: dict[str, Any] | None = None,
    output_shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"version": _TRANSFORM_VERSION, "steps": []}
    if input_shape:
        spec["input_shape"] = input_shape
    if output_shape:
        spec["output_shape"] = output_shape
    return spec


def shape_compatibility(
    current: dict[str, Any],
    reference: dict[str, Any],
) -> tuple[float, list[str]]:
    """Return compatibility score (0-1) and drift warnings."""
    if not reference:
        return 1.0, []
    cur_norm = set(current.get("headers_normalized") or [])
    ref_norm = set(reference.get("headers_normalized") or [])
    if not ref_norm:
        return 1.0, []
    overlap = cur_norm & ref_norm
    score = len(overlap) / max(len(ref_norm), 1)
    drift: list[str] = []
    for h in sorted(ref_norm - cur_norm):
        drift.append(f"Column '{h}' from prior upload is missing")
    for h in sorted(cur_norm - ref_norm):
        drift.append(f"Column '{h}' is new")
    if str(current.get("sheet") or "") != str(reference.get("sheet") or ""):
        drift.append(
            f"Sheet changed from '{reference.get('sheet')}' to '{current.get('sheet')}'"
        )
    return score, drift


def _col_index(headers: list[str], col_name: str) -> int:
    try:
        return headers.index(col_name)
    except ValueError:
        norm = normalize_header_name(col_name)
        for index, header in enumerate(headers):
            if normalize_header_name(header) == norm:
                return index
    return -1


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _apply_group_rows(
    rows: list[list[Any]],
    headers: list[str],
    *,
    key_column: str,
    carry_columns: list[str] | None = None,
    coalesce_columns: list[str] | None = None,
) -> list[list[Any]]:
    key_index = _col_index(headers, key_column)
    if key_index < 0:
        return rows

    carry_indices = [_col_index(headers, name) for name in (carry_columns or [])]
    coalesce_indices = [_col_index(headers, name) for name in (coalesce_columns or [])]

    grouped: list[list[Any]] = []
    current: list[Any] | None = None

    for row in rows:
        values = list(row)
        key_value = values[key_index] if key_index < len(values) else None
        if not _is_blank_value(key_value):
            if current is not None:
                grouped.append(current)
            current = list(values)
            while len(current) < len(headers):
                current.append(None)
            continue

        if current is None:
            continue

        for index in carry_indices:
            if index < 0:
                continue
            while len(current) <= index:
                current.append(None)
            if index < len(values) and not _is_blank_value(values[index]):
                if _is_blank_value(current[index]):
                    current[index] = values[index]

        for index in coalesce_indices:
            if index < 0:
                continue
            while len(current) <= index:
                current.append(None)
            if index < len(values) and not _is_blank_value(values[index]):
                current[index] = values[index]

    if current is not None:
        grouped.append(current)
    return grouped


def _row_dict(headers: list[str], row: list[Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for index, header in enumerate(headers):
        key = str(header)
        data[key] = row[index] if index < len(row) else None
        norm = normalize_header_name(key)
        if norm and norm not in data:
            data[norm] = data[key]
    return data


def _parse_literal(token: str) -> Any:
    token = token.strip()
    if token.lower() == "null":
        return None
    match = _STRING_LITERAL.match(token)
    if match:
        return match.group(1) or match.group(2) or ""
    if re.fullmatch(r"-?\d+(\.\d+)?", token):
        return float(token) if "." in token else int(token)
    if _COL_REF.match(token):
        return token
    return token


def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return (not text) or text.lower() == "null"


def _lookup_value(token: str, row_data: dict[str, Any]) -> Any:
    token = token.strip()
    if not token:
        return None
    if token.lower() == "null":
        return None
    match = _STRING_LITERAL.match(token)
    if match:
        return match.group(1) or match.group(2) or ""
    if re.fullmatch(r"-?\d+(\.\d+)?", token):
        return float(token) if "." in token else int(token)
    if _COL_REF.match(token):
        if token in row_data:
            return row_data[token]
        return row_data.get(normalize_header_name(token))
    return token


def _values_equal(left: Any, right: Any) -> bool:
    if _is_nullish(left) and _is_nullish(right):
        return True
    if _is_nullish(left) or _is_nullish(right):
        return False
    return str(left).strip().lower() == str(right).strip().lower()


def _split_logical(expr: str, separator: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    index = 0
    sep_len = len(separator)
    while index < len(expr):
        char = expr[index]
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            index += 1
            continue
        if expr[index : index + sep_len].lower() == separator.lower():
            parts.append("".join(buf).strip())
            buf = []
            index += sep_len
            continue
        buf.append(char)
        index += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _eval_not_in(expr: str, row_data: dict[str, Any]) -> bool | None:
    match = _NOT_IN.match(expr)
    if not match:
        return None
    value = _lookup_value(match.group(1), row_data)
    items = [item.strip() for item in match.group(2).split(",") if item.strip()]
    return all(not _values_equal(value, _lookup_value(item, row_data)) for item in items)


def _eval_expr(expr: str, row_data: dict[str, Any]) -> Any:
    expr = str(expr or "").strip()
    if not expr:
        return None
    if expr.lower() == "null":
        return None

    or_parts = _split_logical(expr, " or ")
    if len(or_parts) > 1:
        return any(_eval_expr(part, row_data) for part in or_parts)
    or_parts = _split_logical(expr, "||")
    if len(or_parts) > 1:
        return any(_eval_expr(part, row_data) for part in or_parts)

    and_parts = _split_logical(expr, " and ")
    if len(and_parts) > 1:
        return all(bool(_eval_expr(part, row_data)) for part in and_parts)
    and_parts = _split_logical(expr, "&&")
    if len(and_parts) > 1:
        return all(bool(_eval_expr(part, row_data)) for part in and_parts)

    not_in = _eval_not_in(expr, row_data)
    if not_in is not None:
        return not_in

    compare = _COMPARE.match(expr)
    if compare:
        left = _lookup_value(compare.group(1), row_data)
        right = _lookup_value(compare.group(3), row_data)
        equal = _values_equal(left, right)
        return (not equal) if compare.group(2) == "!=" else equal

    if "+" in expr:
        parts = [p.strip() for p in expr.split("+")]
        out = ""
        for part in parts:
            parsed = _parse_literal(part)
            if isinstance(parsed, str) and _COL_REF.match(parsed):
                val = row_data.get(parsed)
                if val is None:
                    val = row_data.get(normalize_header_name(parsed))
                out += "" if val is None else str(val)
            elif isinstance(parsed, str):
                out += parsed
            else:
                out += str(parsed)
        return out
    parsed = _parse_literal(expr)
    if isinstance(parsed, str) and _COL_REF.match(parsed):
        if parsed in row_data:
            return row_data[parsed]
        return row_data.get(normalize_header_name(parsed))
    return parsed


def _cast_value(value: Any, target_type: str) -> tuple[Any, bool]:
    """Cast a single cell to target_type. Never raises — returns (value, ok).

    ok is False when the raw text could not be parsed as target_type, in which
    case the original text is returned so the caller can decide how to handle
    the column (see apply_transformation's "cast" op).
    """
    if value is None or value is _NULL_SENTINEL:
        return None, True
    text = str(value).strip()
    if not text:
        return None, True
    kind = target_type.strip().lower()
    if kind == "string":
        return text, True
    if kind == "number":
        cleaned = text.replace(",", "").replace("$", "")
        try:
            return (float(cleaned) if "." in cleaned else int(cleaned)), True
        except ValueError:
            return text, False
    if kind == "boolean":
        return text.lower() in {"true", "yes", "1", "y"}, True
    if kind == "date":
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat(), True
            except ValueError:
                continue
        return text, False
    if kind == "datetime":
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).isoformat(), True
            except ValueError:
                continue
        return text, False
    if kind in {"currency", "email", "unknown"}:
        return text, True
    return text, True


def apply_transformation(
    rows: list[list[Any]],
    headers: list[str],
    spec: dict[str, Any],
    *,
    issues: list[str] | None = None,
) -> tuple[list[list[Any]], list[str]]:
    """Apply transformation steps to row data; return transformed rows and output headers.

    Never raises on bad cell data — a "cast" step that hits values it cannot
    parse (e.g. a status column that has one row saying "Included") falls back
    to leaving that whole column as text rather than crashing or producing a
    mixed-type column that would later fail to write as Parquet. When `issues`
    is passed, a human-readable note is appended for each column that fell back.
    """
    working_headers = list(headers)
    working_rows = [list(row) for row in rows]

    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        op = str(step.get("op") or "").strip().lower()
        if op == "rename_columns":
            mapping = step.get("mapping") or {}
            if not isinstance(mapping, dict):
                continue
            new_headers = list(working_headers)
            for index, header in enumerate(new_headers):
                if header in mapping:
                    new_headers[index] = str(mapping[header])
            working_headers = new_headers
        elif op == "cast":
            columns = step.get("columns") or {}
            if not isinstance(columns, dict):
                continue
            for col_name, col_type in columns.items():
                try:
                    col_index = working_headers.index(col_name)
                except ValueError:
                    norm = normalize_header_name(col_name)
                    col_index = next(
                        (i for i, h in enumerate(working_headers) if normalize_header_name(h) == norm),
                        -1,
                    )
                if col_index < 0:
                    continue
                cast_results = [_cast_value(row[col_index], str(col_type)) for row in working_rows]
                if all(ok for _value, ok in cast_results):
                    for row, (value, _ok) in zip(working_rows, cast_results):
                        row[col_index] = value
                else:
                    bad_values = sorted(
                        {
                            str(row[col_index])
                            for row, (_value, ok) in zip(working_rows, cast_results)
                            if not ok
                        }
                    )[:3]
                    if issues is not None:
                        issues.append(
                            f"Column '{col_name}' has values that are not valid {col_type} "
                            f"(e.g. {', '.join(bad_values)}); kept as text instead of casting"
                        )
        elif op == "group_rows":
            key_column = str(step.get("key_column") or "").strip()
            if key_column:
                working_rows = _apply_group_rows(
                    working_rows,
                    working_headers,
                    key_column=key_column,
                    carry_columns=[str(name) for name in (step.get("carry_columns") or [])],
                    coalesce_columns=[str(name) for name in (step.get("coalesce_columns") or [])],
                )
        elif op == "filter_rows":
            expr = str(step.get("expr") or "")
            filtered: list[list[Any]] = []
            for row in working_rows:
                row_data = _row_dict(working_headers, row)
                try:
                    if bool(_eval_expr(expr, row_data)):
                        filtered.append(row)
                except Exception:  # noqa: BLE001
                    filtered.append(row)
            working_rows = filtered
        elif op == "derive_column":
            name = str(step.get("name") or "").strip()
            expr = str(step.get("expr") or "")
            if not name:
                continue
            working_headers.append(name)
            for row in working_rows:
                row_data = _row_dict(working_headers[:-1], row)
                try:
                    row.append(_eval_expr(expr, row_data))
                except Exception:  # noqa: BLE001
                    row.append(None)

    output_shape = spec.get("output_shape") or {}
    schema = output_shape.get("schema") or []
    if schema:
        out_names = [str(col.get("name") or "") for col in schema if isinstance(col, dict)]
        out_names = [n for n in out_names if n]
        if out_names:
            return _project_to_output_headers(working_rows, working_headers, out_names)

    return working_rows, working_headers


def _project_to_output_headers(
    working_rows: list[list[Any]],
    working_headers: list[str],
    out_names: list[str],
) -> tuple[list[list[Any]], list[str]]:
    index_by_norm: dict[str, int] = {}
    for index, header in enumerate(working_headers):
        index_by_norm.setdefault(normalize_header_name(header), index)
    mapped = [index_by_norm.get(normalize_header_name(name), -1) for name in out_names]
    if all(index < 0 for index in mapped):
        return working_rows, working_headers
    out_rows: list[list[Any]] = []
    for row in working_rows:
        out_rows.append(
            [row[index] if 0 <= index < len(row) else None for index in mapped]
        )
    return out_rows, out_names


def preview_transformation(
    rows: list[list[Any]],
    headers: list[str],
    spec: dict[str, Any],
    *,
    max_rows: int = 25,
) -> dict[str, Any]:
    """Return before/after preview for a transformation spec."""
    before = {
        "headers": list(headers),
        "rows": rows[:max_rows],
        "row_count": len(rows),
        "preview_row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
    }
    out_rows, out_headers = apply_transformation(rows, headers, spec)
    after = {
        "headers": out_headers,
        "rows": out_rows[:max_rows],
        "row_count": len(out_rows),
        "preview_row_count": min(len(out_rows), max_rows),
        "truncated": len(out_rows) > max_rows,
    }
    return {"before": before, "after": after}
