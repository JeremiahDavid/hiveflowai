"""Heuristic detection of table-like regions in a sheet.

Deterministic first pass: find every rectangular blob of content, trim the ragged
edges, and classify its shape. Ambiguous blobs still come back (low confidence)
for the agent to adjudicate — nothing is silently dropped.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass, field

from hiveflow_spreadsheet_parser.grid import occupancy
from hiveflow_spreadsheet_parser.readers.base import SheetGrid, cell_a1

TOTAL_RE = re.compile(r"^\s*(grand\s+)?(total|totals|sum|subtotal)\b", re.IGNORECASE)

Kind = str  # "table" | "matrix" | "key_value" | "list" | "unknown"


@dataclass(slots=True)
class Region:
    sheet: str
    bbox: tuple[int, int, int, int]  # raw component bounds, 0-indexed inclusive
    core: tuple[int, int, int, int]  # ragged edges trimmed
    a1_range: str
    kind: Kind
    confidence: float
    header_rows: int
    orientation: str = "rows"
    signals: list[str] = field(default_factory=list)
    features: dict[str, object] = field(default_factory=dict)


def _bridge(occ: list[list[bool]], max_gap: int = 1) -> list[list[bool]]:
    """Fill short gaps that sit between content on both sides (same row or col).

    Bridges single blank separator rows (business-system exports) and merged-header
    gaps without expanding the outer boundary of a region.
    """
    h = len(occ)
    w = len(occ[0]) if h else 0
    out = [row[:] for row in occ]

    for r in range(h):
        for c in range(w):
            if occ[r][c]:
                continue
            left = any(occ[r][c - k] for k in range(1, max_gap + 1) if c - k >= 0)
            right = any(occ[r][c + k] for k in range(1, max_gap + 1) if c + k < w)
            up = any(occ[r - k][c] for k in range(1, max_gap + 1) if r - k >= 0)
            down = any(occ[r + k][c] for k in range(1, max_gap + 1) if r + k < h)
            if (left and right) or (up and down):
                out[r][c] = True
    return out


def _components(mask: list[list[bool]]) -> list[tuple[int, int, int, int]]:
    h = len(mask)
    w = len(mask[0]) if h else 0
    seen = [[False] * w for _ in range(h)]
    boxes: list[tuple[int, int, int, int]] = []
    for r in range(h):
        for c in range(w):
            if not mask[r][c] or seen[r][c]:
                continue
            r0 = r1 = r
            c0 = c1 = c
            q: deque[tuple[int, int]] = deque([(r, c)])
            seen[r][c] = True
            while q:
                cr, cc = q.popleft()
                r0, r1 = min(r0, cr), max(r1, cr)
                c0, c1 = min(c0, cc), max(c1, cc)
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < h and 0 <= nc < w and mask[nr][nc] and not seen[nr][nc]:
                        seen[nr][nc] = True
                        q.append((nr, nc))
            boxes.append((r0, c0, r1, c1))
    return boxes


def _trim_core(occ: list[list[bool]], box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Drop leading/trailing rows and columns that are much sparser than the body."""
    r0, c0, r1, c1 = box
    width = c1 - c0 + 1
    height = r1 - r0 + 1
    row_fill = [sum(occ[r][c] for c in range(c0, c1 + 1)) / width for r in range(r0, r1 + 1)]
    col_fill = [sum(occ[r][c] for r in range(r0, r1 + 1)) / height for c in range(c0, c1 + 1)]

    body_med = sorted(row_fill)[len(row_fill) // 2] if row_fill else 0.0
    thresh = max(0.34, body_med * 0.5)

    top = 0
    while top < len(row_fill) - 1 and row_fill[top] < thresh:
        top += 1
    bot = len(row_fill) - 1
    while bot > top and row_fill[bot] < thresh:
        bot -= 1

    col_med = sorted(col_fill)[len(col_fill) // 2] if col_fill else 0.0
    cthresh = max(0.2, col_med * 0.4)
    left = 0
    while left < len(col_fill) - 1 and col_fill[left] < cthresh:
        left += 1
    right = len(col_fill) - 1
    while right > left and col_fill[right] < cthresh:
        right -= 1

    return r0 + top, c0 + left, r0 + bot, c0 + right


def _row_crosses_hmerge(grid: SheetGrid, row: int, c0: int, c1: int) -> bool:
    return any(
        mr0 <= row <= mr1 and mc1 > mc0 and mc0 >= c0 and mc1 <= c1 + 1
        for mr0, mc0, mr1, mc1 in grid.merged_ranges
    )


def _looks_like_header(
    grid: SheetGrid, row: int, c0: int, c1: int, body_styles: Counter[int]
) -> bool:
    text = filled = distinct_style = 0
    for c in range(c0, c1 + 1):
        cell = grid.cell(row, c)
        if not cell.occupied:
            continue
        filled += 1
        if cell.kind == "text":
            text += 1
        if cell.style_key and body_styles and cell.style_key not in body_styles:
            distinct_style += 1
    if filled == 0:
        return False
    span = c1 - c0 + 1
    # A merged, all-text banner row (e.g. "Q1" spanning two columns) is a header
    # even though it fills few cells.
    if _row_crosses_hmerge(grid, row, c0, c1) and text == filled:
        return True
    return (text / span >= 0.6) and (text >= filled - 1) and (distinct_style >= 1 or text == span)


def _classify(grid: SheetGrid, core: tuple[int, int, int, int]) -> Region:
    r0, c0, r1, c1 = core
    n_rows = r1 - r0 + 1
    n_cols = c1 - c0 + 1
    occ = occupancy(grid)

    body_styles: Counter[int] = Counter()
    for r in range(min(r0 + 2, r1), r1 + 1):
        for c in range(c0, c1 + 1):
            k = grid.cell(r, c).style_key
            if k:
                body_styles[k] += 1

    header_rows = 0
    for r in range(r0, min(r0 + 3, r1)):
        if _looks_like_header(grid, r, c0, c1, body_styles):
            header_rows += 1
        else:
            break
    header_rows = max(header_rows, 1) if n_rows >= 2 else 0

    body0 = r0 + header_rows
    # Row-shape consistency over non-empty body rows (raw view, so bridged gap
    # cells and blank spacer rows don't distort the count).
    raw_occ = occupancy(grid)
    col_counts = [
        sum(raw_occ[r][c] for c in range(c0, c1 + 1))
        for r in range(body0, r1 + 1)
        if any(raw_occ[r][c] for c in range(c0, c1 + 1))
    ]
    modal = Counter(col_counts).most_common(1)[0][0] if col_counts else 0
    consistency = sum(1 for x in col_counts if x == modal) / len(col_counts) if col_counts else 0.0

    first_col_text = 0
    first_col_total = 0
    for r in range(body0, r1 + 1):
        cell = grid.cell(r, c0)
        if cell.occupied:
            first_col_total += 1
            if cell.kind == "text":
                first_col_text += 1
    first_col_all_text = first_col_total >= 2 and first_col_text >= first_col_total - 0

    last_first = grid.cell(r1, c0)
    has_totals_row = bool(last_first.kind == "text" and TOTAL_RE.match(str(last_first.value)))
    last_col_header = grid.cell(r0, c1)
    has_totals_col = bool(
        last_col_header.kind == "text" and TOTAL_RE.match(str(last_col_header.value))
    )

    top_row_text = sum(1 for c in range(c0, c1 + 1) if grid.cell(r0, c).kind == "text")
    body_num = body_cells = 0
    for r in range(body0, r1 + 1):
        for c in range(c0 + 1, c1 + 1):
            cell = grid.cell(r, c)
            if cell.occupied:
                body_cells += 1
                if cell.kind in ("number", "date", "bool"):
                    body_num += 1
    body_numeric_ratio = body_num / body_cells if body_cells else 0.0
    pivot_sig = (
        first_col_all_text
        and top_row_text >= n_cols - 1
        and has_totals_row
        and has_totals_col
        and body_numeric_ratio >= 0.8
        and n_cols >= 3
    )

    # alternating blank data rows (pre-bridge view)
    body_rows_filled = [any(raw_occ[r][c] for c in range(c0, c1 + 1)) for r in range(body0, r1 + 1)]
    alt = len(body_rows_filled) >= 6 and all(
        body_rows_filled[i] != body_rows_filled[i + 1] for i in range(len(body_rows_filled) - 1)
    )

    signals: list[str] = []
    if pivot_sig:
        kind = "matrix"
        signals.append("pivot signature: label column + label header row + a total")
    elif n_cols == 2 and first_col_all_text and n_rows >= 3:
        kind = "key_value"
        signals.append("two columns, text keys")
    elif n_cols >= 2 and n_rows >= 2 and consistency >= 0.6:
        kind = "table"
        signals.append(f"{consistency:.0%} of body rows have {modal} values")
    elif n_cols == 1:
        kind = "list"
        signals.append("single column")
    else:
        kind = "unknown"
        signals.append("no consistent row shape")

    if header_rows:
        signals.append(f"{header_rows} header row(s)")
    if has_totals_row:
        signals.append("trailing total row")
    if alt:
        signals.append("blank row between every record")

    conf = 0.45
    area_fill = sum(occ[r][c] for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)) / max(
        1, n_rows * n_cols
    )
    if kind in ("table", "matrix"):
        conf += 0.2
    if consistency >= 0.9:
        conf += 0.15
    if area_fill >= 0.85:
        conf += 0.12
    if header_rows:
        conf += 0.08
    if kind == "unknown":
        conf -= 0.25
    conf = round(min(0.99, max(0.05, conf)), 2)

    return Region(
        sheet=grid.name,
        bbox=core,
        core=core,
        a1_range=f"{cell_a1(r0, c0)}:{cell_a1(r1, c1)}",
        kind=kind,
        confidence=conf,
        header_rows=header_rows,
        orientation="rows",
        signals=signals,
        features={
            "n_rows": n_rows,
            "n_cols": n_cols,
            "row_shape_consistency": round(consistency, 3),
            "area_fill": round(area_fill, 3),
            "body_numeric_ratio": round(body_numeric_ratio, 3),
            "first_col_all_text": first_col_all_text,
            "has_totals_row": has_totals_row,
            "has_totals_col": has_totals_col,
            "alternating_blank_rows": alt,
        },
    )


def detect_sheet(grid: SheetGrid, *, min_cells: int = 4) -> list[Region]:
    occ = occupancy(grid)
    if not occ or not any(any(row) for row in occ):
        return []
    bridged = _bridge(occ, max_gap=1)
    regions: list[Region] = []
    for box in _components(bridged):
        core = _trim_core(occ, box)
        r0, c0, r1, c1 = core
        if (r1 - r0 + 1) * (c1 - c0 + 1) < min_cells or r1 < r0 or c1 < c0:
            continue
        regions.append(_classify(grid, core))
    regions.sort(key=lambda x: (x.core[0], x.core[1]))
    return regions


def detect_workbook(sheets: list[SheetGrid]) -> dict[str, list[Region]]:
    return {s.name: detect_sheet(s) for s in sheets}
