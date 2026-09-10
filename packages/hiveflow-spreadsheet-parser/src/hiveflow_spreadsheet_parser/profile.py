"""Profile an extracted table: dtypes, cardinality, formats, candidate keys."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from itertools import combinations
from typing import Any

from hiveflow_spreadsheet_parser.extract import Extraction
from hiveflow_spreadsheet_parser.models import ColumnProfile, DType, Semantic, TableProfile

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NUM_CLEAN_RE = re.compile(r"[,$€£¥%\s]")
_ID_NAME_RE = re.compile(r"(^|_)(id|uuid|guid|code|number|no|key)$|^id($|_)", re.IGNORECASE)
_CUR_NAME_RE = re.compile(
    r"amount|amt|price|cost|revenue|sales|salary|balance|total|usd|paid|spend|budget",
    re.IGNORECASE,
)
_PCT_NAME_RE = re.compile(r"rate|pct|percent|ratio|margin|share", re.IGNORECASE)


def is_percent_format(fmt: str | None) -> bool:
    return fmt is not None and "%" in fmt


def is_currency_format(fmt: str | None) -> bool:
    return fmt is not None and any(s in fmt for s in ("$", "€", "£", "¥", "USD", "EUR", "GBP"))


def is_date_format(fmt: str | None) -> bool:
    if not fmt or is_percent_format(fmt):
        return False
    low = fmt.lower()
    return any(t in low for t in ("yy", "dd", "mmm")) or low in ("d", "m", "mm")


def _clean_num(text: str) -> float | None:
    s = _NUM_CLEAN_RE.sub("", text.strip())
    if s in ("", "-", "."):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return _clean_num(value)
    return None


def _jsonify(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _non_null(values: list[Any]) -> list[Any]:
    return [v for v in values if v not in (None, "") and not (isinstance(v, float) and v != v)]


def _infer_dtype(values: list[Any], fmt: str | None) -> DType:
    if not values:
        return "string"
    if all(isinstance(v, bool) for v in values):
        return "bool"
    if is_percent_format(fmt):
        return "float"
    if is_currency_format(fmt):
        return "decimal"
    if all(isinstance(v, dt.datetime | dt.date) and not isinstance(v, bool) for v in values):
        only_dates = all(
            isinstance(v, dt.date) and not isinstance(v, dt.datetime) for v in values
        ) or all(isinstance(v, dt.datetime) and v.time() == dt.time() for v in values)
        return "date" if only_dates else "datetime"
    nums = [as_number(v) for v in values]
    if all(n is not None for n in nums):
        if all(float(n).is_integer() for n in nums):  # type: ignore[arg-type]
            return "int"
        return "float"
    return "string"


def _guess_semantic(
    name: str, dtype: DType, values: list[Any], fmt: str | None, *, unique: bool, n: int
) -> Semantic:
    if is_percent_format(fmt) or _PCT_NAME_RE.search(name):
        return "percent"
    if is_currency_format(fmt) or (
        _CUR_NAME_RE.search(name) and dtype in ("float", "decimal", "int")
    ):
        return "currency"
    if dtype in ("date", "datetime"):
        return dtype
    if dtype == "bool":
        return "boolean"
    strs = [v for v in values if isinstance(v, str)]
    if strs and all(_EMAIL_RE.match(v) for v in strs):
        return "email"
    if _ID_NAME_RE.search(name) and (unique or dtype == "int"):
        return "id"
    if dtype in ("int", "float", "decimal"):
        return "number"
    if dtype == "string":
        distinct = len(set(strs))
        avg_len = sum(len(v) for v in strs) / len(strs) if strs else 0
        if n and distinct <= max(2, 0.3 * n) and avg_len <= 40:
            return "category"
        return "free_text"
    return "unknown"


def _column_profile(
    idx: int, name: str, raw: list[Any], fmt: str | None, n_rows: int
) -> ColumnProfile:
    values = _non_null(raw)
    null_count = n_rows - len(values)
    distinct = {(_jsonify(v) if not isinstance(v, str) else v) for v in values}
    distinct_count = len(distinct)
    is_unique = distinct_count == len(values) and null_count == 0 and n_rows > 0

    dtype = _infer_dtype(values, fmt)
    semantic = _guess_semantic(name, dtype, values, fmt, unique=is_unique, n=n_rows)

    prof = ColumnProfile(
        source_name=name,
        name=name,
        ordinal=idx,
        dtype=dtype,
        semantic=semantic,
        nullable=null_count > 0,
        null_count=null_count,
        populated_pct=round(100.0 * len(values) / n_rows, 1) if n_rows else 0.0,
        distinct_count=distinct_count,
        is_unique=is_unique,
        detected_format=fmt,
        sample_values=[_jsonify(v) for v in list(dict.fromkeys(values))[:5]],
        top_values=[(_jsonify(v), c) for v, c in Counter(map(_jsonify, values)).most_common(5)],
    )

    nums = [as_number(v) for v in values]
    if dtype in ("int", "float", "decimal") and nums and all(x is not None for x in nums):
        clean = [x for x in nums if x is not None]
        prof.min = _jsonify(min(clean))
        prof.max = _jsonify(max(clean))
        prof.mean = round(sum(clean) / len(clean), 4)
    elif dtype in ("date", "datetime") and values:
        dates = [v for v in values if isinstance(v, dt.date)]
        lo, hi = min(dates), max(dates)
        if dtype == "date":
            lo = lo.date() if isinstance(lo, dt.datetime) else lo
            hi = hi.date() if isinstance(hi, dt.datetime) else hi
        prof.min = _jsonify(lo)
        prof.max = _jsonify(hi)
    elif dtype == "string" and values:
        lengths = [len(str(v)) for v in values]
        prof.char_min = min(lengths)
        prof.char_max = max(lengths)
    return prof


def _candidate_key(
    columns: list[str], grid: list[list[Any]], profiles: list[ColumnProfile]
) -> list[str]:
    n = len(grid)
    if n == 0:
        return []
    for p in profiles:
        if p.is_unique:
            return [p.name]
    idx_by_name = {c: i for i, c in enumerate(columns)}
    eligible = [p.name for p in profiles if p.null_count == 0 and p.distinct_count > 1]
    eligible.sort(key=lambda name: -profiles[idx_by_name[name]].distinct_count)
    for combo in combinations(eligible[:6], 2):
        idxs = [idx_by_name[c] for c in combo]
        seen = {tuple(_jsonify(row[i]) for i in idxs) for row in grid}
        if len(seen) == n:
            return list(combo)
    return []


def profile_rows(
    columns: list[str],
    rows: list[list[Any]],
    number_formats: list[str | None] | None = None,
    *,
    header_rows: int = 1,
    dropped_blank: int = 0,
    dropped_total: int = 0,
) -> tuple[TableProfile, list[ColumnProfile]]:
    """Profile a bare column/row grid (used for the post-cleanup `apply` report)."""
    fmts = number_formats or [None] * len(columns)
    ex = Extraction(
        columns=columns,
        number_formats=fmts,
        rows=rows,
        records=[],
        n_data_rows=len(rows),
        dropped_blank_rows=dropped_blank,
        dropped_total_rows=dropped_total,
        header_rows=header_rows,
    )
    return profile_extraction(ex)


def profile_extraction(ex: Extraction) -> tuple[TableProfile, list[ColumnProfile]]:
    n_rows = ex.n_data_rows
    cols = ex.columns
    by_col: list[list[Any]] = [
        [row[i] if i < len(row) else None for row in ex.rows] for i in range(len(cols))
    ]
    profiles = [
        _column_profile(i, cols[i], by_col[i], ex.number_formats[i], n_rows)
        for i in range(len(cols))
    ]
    dup = 0
    if n_rows:
        seen: set[tuple[Any, ...]] = set()
        for row in ex.rows:
            key = tuple(_jsonify(v) for v in row)
            if key in seen:
                dup += 1
            else:
                seen.add(key)

    table = TableProfile(
        row_count=n_rows,
        column_count=len(cols),
        empty_row_count=ex.dropped_blank_rows,
        duplicate_row_count=dup,
        candidate_key=_candidate_key(cols, ex.rows, profiles),
        header_rows=ex.header_rows,
        has_totals_row=ex.dropped_total_rows > 0,
    )
    return table, profiles
