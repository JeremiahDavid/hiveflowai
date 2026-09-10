"""Profiling: dtype/semantic inference, cardinality, formats, candidate keys."""

from __future__ import annotations

from hiveflow_spreadsheet_parser.extract import ExtractOptions, extract_region
from hiveflow_spreadsheet_parser.profile import profile_extraction


def _profile(fx, name, a1, header_rows=1):
    grid = fx(name).sheets[0]
    ex = extract_region(grid, a1, ExtractOptions(header_rows=header_rows))
    return profile_extraction(ex)


def test_formats_dtypes_and_semantics(fx):
    _, cols = _profile(fx, "formats.xlsx", "A1:F6")
    by = {c.name: c for c in cols}
    assert by["invoice_id"].dtype == "int" and by["invoice_id"].semantic == "id"
    assert by["issued"].dtype == "date"
    assert by["amount"].dtype == "decimal" and by["amount"].semantic == "currency"
    assert by["tax_rate"].dtype == "float" and by["tax_rate"].semantic == "percent"
    assert by["paid"].dtype == "bool"
    assert by["amount"].detected_format == '"$"#,##0.00'


def test_formats_duplicate_row_detected(fx):
    table, _ = _profile(fx, "formats.xlsx", "A1:F6")
    assert table.duplicate_row_count == 1
    assert table.candidate_key == []  # invoice_id not unique because of the dup


def test_alternating_blank_cardinality(fx):
    table, cols = _profile(fx, "alternating_blank.xlsx", "A1:C24")
    assert table.row_count == 12
    by = {c.name: c for c in cols}
    assert by["status"].distinct_count == 3
    assert by["status"].semantic == "category"
    assert by["ticket"].is_unique
    assert table.candidate_key == ["ticket"]


def test_clean_single_candidate_key(fx):
    table, _ = _profile(fx, "clean_single.xlsx", "A1:E21")
    assert table.candidate_key == ["order_id"]
    assert table.row_count == 20
