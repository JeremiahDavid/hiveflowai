"""xlsx reader + grid primitives."""

from __future__ import annotations

from hiveflow_spreadsheet_parser.grid import occupancy, overview
from hiveflow_spreadsheet_parser.readers.base import col_letter, parse_a1_range


def test_a1_helpers():
    assert col_letter(0) == "A"
    assert col_letter(26) == "AA"
    assert parse_a1_range("B2:D10") == (1, 1, 9, 3)
    assert parse_a1_range("D10:B2") == (1, 1, 9, 3)  # normalized
    assert parse_a1_range("C5") == (4, 2, 4, 2)


def test_reader_captures_merges_and_trims(fx):
    grid = fx("merged_headers.xlsx").sheets[0]
    assert grid.name == "Budget"
    assert len(grid.merged_ranges) == 4
    assert grid.nrows == 8 and grid.ncols == 5  # trailing empties trimmed


def test_blank_strings_are_empty(fx):
    grid = fx("two_stacked.xlsx").sheets[0]
    occ = occupancy(grid)
    assert not any(occ[6])  # the spacer row A7 is fully empty


def test_overview_shape_map_smoke(fx):
    ov = overview(fx("side_by_side.xlsx").sheets[0])
    assert ov.nrows == 11
    assert "A" in ov.shape_map
    assert ov.density > 0
