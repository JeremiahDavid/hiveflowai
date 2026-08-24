"""Shared column-profiling primitives (type inference, patterns, stats).

Used by both Spreadsheet Engine (`hiveflow.spreadsheet.profiler`, row-sampled
workbook columns) and the DNA data profiling engine (`hiveflow.dna.data_profile`,
parquet-sampled lake columns). Pure functions, no I/O, no table/row-shape
assumptions — operates on a flat list of column values.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
CURRENCY_RE = re.compile(r"^[\$€£]?\s?-?\d[\d,]*(\.\d+)?$")


def infer_type(values: list[Any]) -> str:
    non_null = [v for v in values if v is not None and str(v).strip() != ""]
    if not non_null:
        return "unknown"
    if all(isinstance(v, bool) for v in non_null):
        return "boolean"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "number"
    if all(isinstance(v, (date, datetime)) for v in non_null):
        return "datetime"
    str_vals = [str(v).strip() for v in non_null]
    if all(DATE_RE.match(v) for v in str_vals):
        return "date"
    if all(EMAIL_RE.match(v) for v in str_vals):
        return "email"
    if all(CURRENCY_RE.match(v) for v in str_vals):
        return "currency"
    numeric = 0
    for value in str_vals:
        cleaned = value.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
        try:
            float(cleaned)
            numeric += 1
        except ValueError:
            pass
    if numeric / len(str_vals) >= 0.9:
        return "number"
    return "string"


def detect_patterns(values: list[Any]) -> list[str]:
    patterns: list[str] = []
    str_vals = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not str_vals:
        return patterns
    if all(EMAIL_RE.match(v) for v in str_vals):
        patterns.append("email")
    if all(DATE_RE.match(v) for v in str_vals):
        patterns.append("iso_date")
    if all(CURRENCY_RE.match(v) for v in str_vals):
        patterns.append("currency")
    return patterns


def profile_column(name: str, values: list[Any]) -> dict[str, Any]:
    """Profile one column's values: type, null rate, cardinality, key/pattern hints."""
    non_null = [v for v in values if v is not None and str(v).strip() != ""]
    distinct = {str(v) for v in non_null}
    cardinality = len(distinct)
    inferred = infer_type(non_null)
    unique_ratio = cardinality / len(non_null) if non_null else 0.0
    likely_key = bool(non_null) and unique_ratio >= 0.95 and cardinality >= 2
    top_values = Counter(str(v) for v in non_null).most_common(5)
    return {
        "name": name,
        "inferred_type": inferred,
        "null_rate": 1.0 - (len(non_null) / len(values)) if values else 1.0,
        "cardinality": cardinality,
        "unique_ratio": round(unique_ratio, 4),
        "likely_key": likely_key,
        "patterns": detect_patterns(non_null),
        "sample_values": [str(v) for v in non_null[:5]],
        "top_values": [{"value": val, "count": count} for val, count in top_values],
    }
