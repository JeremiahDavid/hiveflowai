"""Deterministic region detection over the messy fixtures."""

from __future__ import annotations

import pytest

from hiveflow_spreadsheet_parser.detect import detect_sheet

pytestmark = pytest.mark.filterwarnings("ignore")


def _regions(fx, name):
    wb = fx(name)
    return detect_sheet(wb.sheets[0])


def test_clean_single_one_table(fx):
    (r,) = _regions(fx, "clean_single.xlsx")
    assert r.kind == "table"
    assert r.a1_range == "A1:E21"
    assert r.header_rows == 1
    assert r.confidence >= 0.9


def test_two_stacked_split_into_two(fx):
    regions = _regions(fx, "two_stacked.xlsx")
    assert [x.a1_range for x in regions] == ["A1:C5", "A10:D16"]
    assert all(x.kind == "table" for x in regions)


def test_side_by_side_not_merged(fx):
    regions = _regions(fx, "side_by_side.xlsx")
    assert len(regions) == 2
    assert {x.a1_range for x in regions} == {"A1:C11", "F1:H11"}


def test_report_export_trims_title_banner(fx):
    (r,) = _regions(fx, "report_export.xlsx")
    assert r.a1_range.startswith("A5:")
    assert r.kind == "table"  # a total row does not make it a matrix
    assert r.features["has_totals_row"] is True


def test_merged_headers_two_header_rows(fx):
    (r,) = _regions(fx, "merged_headers.xlsx")
    assert r.header_rows == 2
    assert r.kind == "table"


def test_alternating_blank_is_one_table(fx):
    (r,) = _regions(fx, "alternating_blank.xlsx")
    assert r.kind == "table"
    assert r.features["alternating_blank_rows"] is True


def test_pivot_matrix_classified_matrix(fx):
    (r,) = _regions(fx, "pivot_matrix.xlsx")
    assert r.kind == "matrix"
    assert r.features["has_totals_row"] and r.features["has_totals_col"]
