"""Apply-phase: turn an approved review entry into a cleaned DataFrame."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pandas as pd

from hiveflow_spreadsheet_parser.extract import ExtractOptions, extract_region
from hiveflow_spreadsheet_parser.models import ColumnProfile, DraftTable, DType, TableProfile
from hiveflow_spreadsheet_parser.profile import as_number, profile_rows
from hiveflow_spreadsheet_parser.readers.base import SheetGrid

_WS_RE = re.compile(r"\s+")
_TRUE = {"true", "t", "yes", "y", "1", "1.0"}
_FALSE = {"false", "f", "no", "n", "0", "0.0"}


@dataclass(slots=True)
class CleanResult:
    frame: pd.DataFrame
    row_count_in: int
    row_count_out: int
    profile_before: TableProfile
    profile_after: TableProfile
    transforms: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _to_bool(value: object) -> object:
    if isinstance(value, bool) or value is None:
        return value
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _coerce(series: pd.Series, dtype: DType) -> tuple[pd.Series, int]:
    """Return ``(coerced, n_failures)`` where failures became NA."""
    present = series.notna() & (series.astype("object") != "")
    if dtype in ("int", "float", "decimal"):
        out = series.map(lambda v: as_number(v) if v not in (None, "") else None)
        if dtype == "int":
            out = out.map(lambda v: int(v) if v is not None and float(v).is_integer() else v)
        num = pd.to_numeric(out, errors="coerce")
        fails = int((present & num.isna()).sum())
        if dtype == "int":
            return num.astype("Int64"), fails
        return num.astype("float64"), fails
    if dtype == "bool":
        out = series.map(_to_bool)
        fails = int((present & out.isna()).sum())
        return out.astype("boolean"), fails
    if dtype in ("date", "datetime"):
        parsed = pd.to_datetime(series, errors="coerce")
        fails = int((present & parsed.isna()).sum())
        if dtype == "date":
            return parsed.dt.date, fails
        return parsed, fails

    # string
    def _clean_str(v: object) -> object:
        if v is None or (isinstance(v, float) and v != v):
            return None
        s = _WS_RE.sub(" ", str(v).strip())
        return s or None

    return series.map(_clean_str), 0


def clean_table(grid: SheetGrid, table: DraftTable) -> CleanResult:
    ex = extract_region(
        grid,
        table.a1_range,
        ExtractOptions(
            header_rows=table.header_rows,
            orientation=table.orientation,
            row_group_key=table.row_group_key,
        ),
    )
    before, _ = profile_rows(
        ex.columns,
        ex.rows,
        ex.number_formats,
        header_rows=ex.header_rows,
        dropped_blank=ex.dropped_blank_rows,
        dropped_total=ex.dropped_total_rows,
    )
    row_count_in = ex.n_data_rows

    df = pd.DataFrame(ex.rows, columns=ex.columns) if ex.rows else pd.DataFrame(columns=ex.columns)
    transforms: list[str] = []
    warnings: list[str] = list(ex.warnings)
    if ex.merged_row_count:
        transforms.append(
            f"merged {ex.merged_row_count} continuation row(s) into their preceding record "
            f"on {table.row_group_key!r}"
        )

    included = [c for c in table.columns if c.include]
    dropped = [c.source_name for c in table.columns if not c.include]
    if dropped:
        transforms.append(f"dropped {len(dropped)} column(s): {', '.join(dropped)}")

    ordered: list[ColumnProfile] = sorted(included, key=lambda c: c.ordinal)
    out = pd.DataFrame(index=df.index)
    for col in ordered:
        src = col.source_name
        if src not in df.columns:
            warnings.append(f"column {src!r} not found in extracted range; emitted as empty")
            out[col.name] = pd.Series([None] * len(df), index=df.index)
            continue
        coerced, fails = _coerce(df[src], col.dtype)
        out[col.name] = coerced.to_numpy()
        if col.name != src:
            transforms.append(f"renamed {src!r} -> {col.name!r}")
        transforms.append(
            f"coerced {col.name!r} to {col.dtype}"
            + (f" ({fails} value(s) nulled)" if fails else "")
        )
        if fails:
            warnings.append(f"{fails} value(s) in {col.name!r} could not be parsed as {col.dtype}")

    before_rows = len(out)
    if table.drop_duplicate_rows:
        out = out.drop_duplicates(keep="first")
        if len(out) != before_rows:
            transforms.append(f"dropped {before_rows - len(out)} fully-duplicate row(s)")
    if table.dedupe_on:
        keys = [k for k in table.dedupe_on if k in out.columns]
        missing = set(table.dedupe_on) - set(keys)
        if missing:
            warnings.append(f"dedupe_on names not in output schema, ignored: {sorted(missing)}")
        if keys:
            n0 = len(out)
            out = out.drop_duplicates(subset=keys, keep="first")
            transforms.append(f"deduped on {keys}: -{n0 - len(out)} row(s)")

    out = out.reset_index(drop=True)

    after_rows = [list(r) for r in out.itertuples(index=False, name=None)]
    after, _ = profile_rows(list(out.columns), after_rows, header_rows=1)

    return CleanResult(
        frame=out,
        row_count_in=row_count_in,
        row_count_out=len(out),
        profile_before=before,
        profile_after=after,
        transforms=transforms,
        warnings=warnings,
    )


def frame_to_csv_value(value: object) -> object:
    """Normalize a cell for CSV writing (ISO dates, no ``NaT``/``<NA>``)."""
    if value is None or value is pd.NaT or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value
