"""Occupancy, density, and a compact visual map of a sheet's layout."""

from __future__ import annotations

from dataclasses import dataclass

from hiveflow_spreadsheet_parser.readers.base import SheetGrid, col_letter


def occupancy(grid: SheetGrid, *, spread_merges: bool = True) -> list[list[bool]]:
    """Boolean ``nrows x ncols`` matrix of which cells hold content.

    With ``spread_merges`` a merged range counts as occupied across its whole
    span, not just the anchor cell.
    """
    occ = [[grid.cells[r][c].occupied for c in range(grid.ncols)] for r in range(grid.nrows)]
    if spread_merges:
        for r0, c0, r1, c1 in grid.merged_ranges:
            if not grid.cells[r0][c0].occupied:
                continue
            for r in range(r0, min(r1, grid.nrows - 1) + 1):
                for c in range(c0, min(c1, grid.ncols - 1) + 1):
                    occ[r][c] = True
    return occ


def row_fill(occ: list[list[bool]]) -> list[float]:
    if not occ or not occ[0]:
        return []
    width = len(occ[0])
    return [sum(row) / width for row in occ]


def col_fill(occ: list[list[bool]]) -> list[float]:
    if not occ or not occ[0]:
        return []
    height = len(occ)
    return [sum(occ[r][c] for r in range(height)) / height for c in range(len(occ[0]))]


def empty_runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Runs of ``True`` as ``(start, length)`` pairs (used for empty rows/cols)."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - start))
            start = None
    if start is not None:
        runs.append((start, len(flags) - start))
    return runs


_RAMP = " .:#"


def shape_map(grid: SheetGrid, *, max_w: int = 100, max_h: int = 80) -> str:
    """Downsampled ASCII picture of the sheet so an agent can see its layout.

    Each character is a cell (or a pooled block when the sheet is larger than
    ``max_w x max_h``): blank = empty, ``.`` / ``:`` / ``#`` = increasing fill.
    """
    occ = occupancy(grid)
    if not occ or not occ[0]:
        return "(empty sheet)"

    nrows, ncols = grid.nrows, grid.ncols
    bh = max(1, -(-nrows // max_h))  # ceil division
    bw = max(1, -(-ncols // max_w))

    lines: list[str] = []
    header_cols = [col_letter(c * bw) for c in range((ncols + bw - 1) // bw)]
    ruler = "     " + "".join(c[0] if len(c) == 1 else "+" for c in header_cols)
    lines.append(ruler)

    for br in range((nrows + bh - 1) // bh):
        r0 = br * bh
        chars: list[str] = []
        for bc in range((ncols + bw - 1) // bw):
            c0 = bc * bw
            filled = total = 0
            for r in range(r0, min(r0 + bh, nrows)):
                for c in range(c0, min(c0 + bw, ncols)):
                    total += 1
                    filled += occ[r][c]
            ratio = filled / total if total else 0.0
            idx = 0 if ratio == 0 else min(3, 1 + int(ratio * 3))
            chars.append(_RAMP[idx])
        lines.append(f"{r0 + 1:>4} " + "".join(chars))
    return "\n".join(lines)


@dataclass(slots=True)
class SheetOverview:
    name: str
    nrows: int
    ncols: int
    filled_cells: int
    density: float
    n_merged_ranges: int
    empty_row_runs: list[tuple[int, int]]
    shape_map: str

    def summary(self) -> str:
        return (
            f"{self.name}: {self.nrows} rows x {self.ncols} cols, "
            f"{self.filled_cells} filled ({self.density:.0%} dense), "
            f"{self.n_merged_ranges} merged ranges"
        )


def overview(grid: SheetGrid) -> SheetOverview:
    occ = occupancy(grid)
    rf = row_fill(occ)
    filled = sum(sum(row) for row in occ)
    total = grid.nrows * grid.ncols
    return SheetOverview(
        name=grid.name,
        nrows=grid.nrows,
        ncols=grid.ncols,
        filled_cells=filled,
        density=(filled / total if total else 0.0),
        n_merged_ranges=len(grid.merged_ranges),
        empty_row_runs=empty_runs([f == 0 for f in rf]),
        shape_map=shape_map(grid),
    )
