"""Normalization: merged fill, blank-row drop, multi-row headers, total rows."""

from __future__ import annotations

import datetime as dt

from hiveflow_spreadsheet_parser.extract import ExtractOptions, extract_region


def test_merged_headers_flatten_and_forward_fill(fx):
    grid = fx("merged_headers.xlsx").sheets[0]
    ex = extract_region(grid, "A1:E8", ExtractOptions(header_rows=2))
    assert ex.columns == ["category", "q1_plan", "q1_actual", "q2_plan", "q2_actual"]
    # merged row-label column A is forward-filled down each group
    assert [row[0] for row in ex.rows] == ["Marketing"] * 3 + ["Sales"] * 3
    assert ex.n_data_rows == 6


def test_alternating_blank_rows_dropped(fx):
    grid = fx("alternating_blank.xlsx").sheets[0]
    ex = extract_region(grid, "A1:C24", ExtractOptions(header_rows=1))
    assert ex.n_data_rows == 12
    assert ex.dropped_blank_rows == 11
    assert all(row[0] for row in ex.rows)


def test_row_group_key_merges_variable_length_wraps(fx):
    grid = fx("wrapped_records.xlsx").sheets[0]
    ex = extract_region(
        grid, "A1:D11", ExtractOptions(header_rows=1, row_group_key="id")
    )
    assert ex.n_data_rows == 4
    assert ex.merged_row_count == 6  # 10 data rows - 4 records
    assert ex.records == [
        {"id": "W-1", "name": "Alice", "dept": "Eng", "hours": 5},
        {"id": "W-2", "name": "Bob", "dept": "Sales", "hours": 3},
        {"id": "W-3", "name": "Carol", "dept": "Ops", "hours": 8},
        {"id": "W-4", "name": "Dave", "dept": "Support", "hours": 2},
    ]


def test_row_group_key_missing_column_is_a_noop_with_warning(fx):
    grid = fx("wrapped_records.xlsx").sheets[0]
    ex = extract_region(
        grid, "A1:D11", ExtractOptions(header_rows=1, row_group_key="not_a_column")
    )
    assert ex.merged_row_count == 0
    assert ex.n_data_rows == 10
    assert any("not_a_column" in w for w in ex.warnings)


def test_trailing_total_row_dropped(fx):
    grid = fx("report_export.xlsx").sheets[0]
    ex = extract_region(grid, "A5:C12", ExtractOptions(header_rows=1))
    assert ex.dropped_total_rows == 1
    assert ex.n_data_rows == 5
    assert "Grand Total" not in [r[0] for r in ex.rows]


def test_types_survive_extraction(fx):
    grid = fx("formats.xlsx").sheets[0]
    ex = extract_region(grid, "A1:F6", ExtractOptions(header_rows=1))
    first = ex.records[0]
    assert isinstance(first["invoice_id"], int)
    assert isinstance(first["issued"], dt.date)
    assert isinstance(first["paid"], bool)
