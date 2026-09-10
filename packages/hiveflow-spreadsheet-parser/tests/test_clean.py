"""Apply-phase cleanup pipeline: rename, drop, reorder, coerce, dedupe."""

from __future__ import annotations

import datetime as dt

from hiveflow_spreadsheet_parser.clean import clean_table
from hiveflow_spreadsheet_parser.discover import build_draft_table


def _draft(fx, name, a1, header_rows=1):
    grid = fx(name).sheets[0]
    return grid, build_draft_table(grid, a1, header_rows=header_rows)


def test_rename_drop_reorder_and_coerce(fx):
    grid, table = _draft(fx, "formats.xlsx", "A1:F6")
    by = {c.name: c for c in table.columns}
    by["client"].name = "client_name"
    by["paid"].include = False
    by["amount"].ordinal = -1  # move amount first

    result = clean_table(grid, table)
    df = result.frame

    assert "paid" not in df.columns
    assert next(iter(df.columns)) == "amount"
    assert "client_name" in df.columns
    # whitespace trimmed on the string column
    assert df["client_name"].tolist()[:2] == ["Globex", "Initech"]
    # dates coerced to date objects
    assert isinstance(df["issued"].tolist()[0], dt.date)
    assert any("renamed 'client'" in t for t in result.transforms)


def test_dedupe_on_key(fx):
    grid, table = _draft(fx, "formats.xlsx", "A1:F6")
    table.dedupe_on = ["invoice_id"]
    result = clean_table(grid, table)
    assert result.row_count_in == 5
    assert result.row_count_out == 4
    assert any("deduped on ['invoice_id']" in t for t in result.transforms)


def test_drop_duplicate_rows(fx):
    grid, table = _draft(fx, "formats.xlsx", "A1:F6")
    table.drop_duplicate_rows = True
    result = clean_table(grid, table)
    assert result.row_count_out == 4


def test_merged_headers_forward_fill_in_output(fx):
    grid, table = _draft(fx, "merged_headers.xlsx", "A1:E8", header_rows=2)
    result = clean_table(grid, table)
    assert result.frame["category"].tolist() == ["Marketing"] * 3 + ["Sales"] * 3


def test_row_group_key_merges_wrapped_records_in_output(fx):
    grid = fx("wrapped_records.xlsx").sheets[0]
    table = build_draft_table(grid, "A1:D11", header_rows=1, row_group_key="id")
    result = clean_table(grid, table)
    df = result.frame

    assert result.row_count_in == 4
    assert df["id"].tolist() == ["W-1", "W-2", "W-3", "W-4"]
    assert df["hours"].tolist() == [5, 3, 8, 2]
    assert any("merged 6 continuation row(s)" in t for t in result.transforms)
