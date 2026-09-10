"""Turn a detected region + resolved options into normalized, typed rows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from hiveflow_spreadsheet_parser.detect import TOTAL_RE
from hiveflow_spreadsheet_parser.readers.base import SheetGrid, parse_a1_range

_SLUG_RE = re.compile(r"[^0-9a-z]+")


def slugify(text: str, *, fallback: str = "column") -> str:
    s = _SLUG_RE.sub("_", str(text).strip().lower()).strip("_")
    return s or fallback


class ExtractOptions(BaseModel):
    """How to normalize a region. Every field has a sensible default."""

    model_config = ConfigDict(extra="forbid")

    header_rows: int = 1
    orientation: str = "rows"  # "rows" | "columns"
    drop_blank_rows: bool = True
    drop_total_rows: bool = True
    forward_fill_merged: bool = True
    slug_headers: bool = True
    row_group_key: str | None = None  # column that starts a new logical record


@dataclass(slots=True)
class Extraction:
    columns: list[str]
    number_formats: list[str | None]
    rows: list[list[object]]
    records: list[dict[str, object]]
    n_data_rows: int
    dropped_blank_rows: int = 0
    dropped_total_rows: int = 0
    merged_row_count: int = 0
    header_rows: int = 1
    warnings: list[str] = field(default_factory=list)


def _slice_values(
    grid: SheetGrid, box: tuple[int, int, int, int], *, fill_merged: bool
) -> tuple[list[list[object]], list[list[str | None]]]:
    r0, c0, r1, c1 = box
    r1 = min(r1, grid.nrows - 1)
    c1 = min(c1, grid.ncols - 1)
    values = [[grid.cell(r, c).value for c in range(c0, c1 + 1)] for r in range(r0, r1 + 1)]
    fmts = [[grid.cell(r, c).number_format for c in range(c0, c1 + 1)] for r in range(r0, r1 + 1)]
    if fill_merged:
        for mr0, mc0, mr1, mc1 in grid.merged_ranges:
            if mr1 < r0 or mr0 > r1 or mc1 < c0 or mc0 > c1:
                continue
            src = grid.cell(mr0, mc0).value
            src_fmt = grid.cell(mr0, mc0).number_format
            for r in range(max(mr0, r0), min(mr1, r1) + 1):
                for c in range(max(mc0, c0), min(mc1, c1) + 1):
                    values[r - r0][c - c0] = src
                    fmts[r - r0][c - c0] = src_fmt
    return values, fmts


def _flatten_headers(header_block: list[list[object]], width: int, *, slug: bool) -> list[str]:
    names: list[str] = []
    seen: dict[str, int] = {}
    for c in range(width):
        parts = [
            str(header_block[r][c]).strip()
            for r in range(len(header_block))
            if header_block[r][c] not in (None, "")
        ]
        # collapse repeats from merged forward-fill ("Q1", "Q1" -> "Q1")
        dedup: list[str] = []
        for p in parts:
            if not dedup or dedup[-1] != p:
                dedup.append(p)
        raw = " / ".join(dedup) if dedup else f"column_{c + 1}"
        name = slugify(raw, fallback=f"column_{c + 1}") if slug else raw
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        names.append(name)
    return names


def _modal_formats(
    body: list[list[object]], fmts: list[list[str | None]], width: int
) -> list[str | None]:
    out: list[str | None] = []
    for c in range(width):
        counts: dict[str, int] = {}
        for r, row in enumerate(body):
            if c < len(row) and row[c] not in (None, ""):
                f = fmts[r][c] if c < len(fmts[r]) else None
                if f and f != "General":
                    counts[f] = counts.get(f, 0) + 1
        out.append(max(counts, key=counts.get) if counts else None)  # type: ignore[arg-type]
    return out


def _merge_row_groups(
    body_rows: list[list[object]],
    body_fmts: list[list[str | None]],
    columns: list[str],
    key_col: str,
) -> tuple[list[list[object]], list[list[str | None]], int]:
    """Fold rows where ``key_col`` is blank into the most recent row that had it set.

    Handles a record wrapped across an arbitrary number of physical rows (an ID on
    one row, the rest of its fields on however many rows follow) rather than a fixed
    group size: a new record starts whenever ``key_col`` is populated, and every row
    until the next one is treated as continuation data for it.
    """
    if key_col not in columns or not body_rows:
        return body_rows, body_fmts, 0
    key_idx = columns.index(key_col)
    merged_rows: list[list[object]] = []
    merged_fmts: list[list[str | None]] = []
    n_merged = 0
    for row, frow in zip(body_rows, body_fmts, strict=False):
        starts_group = row[key_idx] not in (None, "")
        if starts_group or not merged_rows:
            merged_rows.append(list(row))
            merged_fmts.append(list(frow))
            continue
        if any(v not in (None, "") for v in row):
            n_merged += 1
        target, target_fmt = merged_rows[-1], merged_fmts[-1]
        for i, v in enumerate(row):
            if v not in (None, "") and target[i] in (None, ""):
                target[i] = v
                target_fmt[i] = frow[i] if i < len(frow) else target_fmt[i]
    return merged_rows, merged_fmts, n_merged


def extract_region(
    grid: SheetGrid, a1_range: str, options: ExtractOptions | None = None
) -> Extraction:
    opts = options or ExtractOptions()
    box = parse_a1_range(a1_range)
    values, fmts = _slice_values(grid, box, fill_merged=opts.forward_fill_merged)

    if opts.orientation == "columns":
        values = [list(row) for row in zip(*values, strict=False)]
        fmts = [list(row) for row in zip(*fmts, strict=False)]

    width = max((len(r) for r in values), default=0)
    values = [row + [None] * (width - len(row)) for row in values]
    fmts = [row + [None] * (width - len(row)) for row in fmts]

    hr = max(0, min(opts.header_rows, len(values) - 1))
    header_block: list[list[object]] = (
        values[:hr] if hr else [[f"column_{c + 1}" for c in range(width)]]
    )
    columns = _flatten_headers(header_block, width, slug=opts.slug_headers)

    body_rows = values[hr:]
    body_fmts = fmts[hr:]

    warnings: list[str] = []
    merged_row_count = 0
    if opts.row_group_key:
        key_col = opts.row_group_key
        if key_col not in columns and slugify(key_col) in columns:
            key_col = slugify(key_col)
        if key_col in columns:
            body_rows, body_fmts, merged_row_count = _merge_row_groups(
                body_rows, body_fmts, columns, key_col
            )
        else:
            warnings.append(
                f"row_group_key {opts.row_group_key!r} not found among columns; ignored"
            )

    last_idx = len(body_rows) - 1
    kept: list[list[object]] = []
    kept_fmts: list[list[object]] = []
    dropped_blank = dropped_total = 0
    for i, (row, frow) in enumerate(zip(body_rows, body_fmts, strict=False)):
        if opts.drop_blank_rows and all(v in (None, "") for v in row):
            dropped_blank += 1
            continue
        first = next((v for v in row if v not in (None, "")), None)
        n_filled = sum(1 for v in row if v not in (None, ""))
        is_total = isinstance(first, str) and bool(TOTAL_RE.match(first))
        # A total/subtotal row is one that is labelled as such and is either
        # ragged (fewer values than columns) or the final row of the block.
        if opts.drop_total_rows and is_total and (n_filled < width or i == last_idx):
            dropped_total += 1
            continue
        kept.append(list(row))
        kept_fmts.append(list(frow))

    number_formats = _modal_formats(kept, kept_fmts, width)  # type: ignore[arg-type]
    records = [dict(zip(columns, row, strict=False)) for row in kept]

    return Extraction(
        columns=columns,
        number_formats=number_formats,
        rows=kept,
        records=records,
        n_data_rows=len(kept),
        dropped_blank_rows=dropped_blank,
        dropped_total_rows=dropped_total,
        merged_row_count=merged_row_count,
        header_rows=hr,
        warnings=warnings,
    )


class ExtractionModel(BaseModel):
    """Serializable view of an :class:`Extraction` (used in tool payloads)."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    n_data_rows: int
    dropped_blank_rows: int
    dropped_total_rows: int
    header_rows: int
    sample: list[dict[str, object]] = Field(default_factory=list)

    @classmethod
    def from_extraction(cls, ex: Extraction, *, sample: int = 8) -> ExtractionModel:
        return cls(
            columns=ex.columns,
            n_data_rows=ex.n_data_rows,
            dropped_blank_rows=ex.dropped_blank_rows,
            dropped_total_rows=ex.dropped_total_rows,
            header_rows=ex.header_rows,
            sample=ex.records[:sample],
        )
