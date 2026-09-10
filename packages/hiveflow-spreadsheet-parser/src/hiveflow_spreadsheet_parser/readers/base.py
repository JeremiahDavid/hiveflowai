"""The common in-memory model every reader produces: a dense cell `Grid`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

CellKind = Literal["empty", "text", "number", "date", "bool", "formula", "error"]

_A1_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")


def col_letter(col0: int) -> str:
    """0-indexed column number -> spreadsheet letters (0 -> ``A``, 26 -> ``AA``)."""
    if col0 < 0:
        raise ValueError("column index must be >= 0")
    n = col0 + 1
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def col_index(letters: str) -> int:
    """``A`` -> 0, ``AA`` -> 26."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def cell_a1(row0: int, col0: int) -> str:
    return f"{col_letter(col0)}{row0 + 1}"


def parse_a1_cell(ref: str) -> tuple[int, int]:
    """``C5`` -> ``(4, 2)`` (0-indexed row, col)."""
    m = _A1_CELL.match(ref.strip())
    if not m:
        raise ValueError(f"not an A1 cell reference: {ref!r}")
    return int(m.group(2)) - 1, col_index(m.group(1))


def parse_a1_range(ref: str) -> tuple[int, int, int, int]:
    """``B2:D10`` -> ``(1, 1, 9, 3)`` as ``(r0, c0, r1, c1)`` inclusive, normalized."""
    ref = ref.strip()
    if ":" not in ref:
        r, c = parse_a1_cell(ref)
        return r, c, r, c
    a, b = ref.split(":", 1)
    r0, c0 = parse_a1_cell(a)
    r1, c1 = parse_a1_cell(b)
    return min(r0, r1), min(c0, c1), max(r0, r1), max(c0, c1)


@dataclass(slots=True)
class Cell:
    """One grid cell with just enough metadata for structure detection."""

    value: Any = None
    kind: CellKind = "empty"
    number_format: str | None = None
    is_merged: bool = False  # covered by a merged range (any position)
    merge_origin: bool = False  # top-left anchor of a merged range
    style_key: int = 0  # cheap hash of visual style, for header/body contrast

    @property
    def occupied(self) -> bool:
        return self.kind != "empty"


@dataclass(slots=True)
class SheetGrid:
    """A single sheet as a dense ``nrows x ncols`` array of :class:`Cell`."""

    name: str
    nrows: int
    ncols: int
    cells: list[list[Cell]]
    merged_ranges: list[tuple[int, int, int, int]] = field(default_factory=list)
    hidden_rows: set[int] = field(default_factory=set)
    hidden_cols: set[int] = field(default_factory=set)
    frozen_rows: int = 0
    frozen_cols: int = 0

    def cell(self, row0: int, col0: int) -> Cell:
        if 0 <= row0 < self.nrows and 0 <= col0 < self.ncols:
            return self.cells[row0][col0]
        return Cell()

    def range_a1(self, r0: int, c0: int, r1: int, c1: int) -> str:
        return f"{cell_a1(r0, c0)}:{cell_a1(r1, c1)}"

    def iter_block(self, r0: int, c0: int, r1: int, c1: int) -> list[list[Cell]]:
        r1 = min(r1, self.nrows - 1)
        c1 = min(c1, self.ncols - 1)
        return [self.cells[r][c0 : c1 + 1] for r in range(r0, r1 + 1)]


@dataclass(slots=True)
class Workbook:
    path: str
    fmt: str
    sheets: list[SheetGrid]

    @property
    def sheet_names(self) -> list[str]:
        return [s.name for s in self.sheets]

    def sheet(self, name: str) -> SheetGrid:
        for s in self.sheets:
            if s.name == name:
                return s
        raise KeyError(f"no sheet named {name!r}; have {self.sheet_names}")


@runtime_checkable
class WorkbookReader(Protocol):
    """Read a file into a :class:`Workbook`. One implementation per format."""

    extensions: tuple[str, ...]

    def read(self, path: str | Path) -> Workbook: ...
