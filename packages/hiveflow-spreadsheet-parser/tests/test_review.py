"""review.yaml round-trip + semantic validation + end-to-end apply."""

from __future__ import annotations

import pytest

from hiveflow_spreadsheet_parser.discover import draft_for_file
from hiveflow_spreadsheet_parser.models import DraftManifest
from hiveflow_spreadsheet_parser.readers import read_workbook
from hiveflow_spreadsheet_parser.review import (
    ReviewError,
    apply_review,
    dump_review,
    load_review,
    validate_review,
)


def test_yaml_round_trip(fx, fixtures_dir, tmp_path):
    manifest = draft_for_file(str(fixtures_dir / "two_stacked.xlsx"))
    path = dump_review(manifest, tmp_path / "two_stacked.xlsx.review.yaml")
    reloaded = load_review(path)
    assert isinstance(reloaded, DraftManifest)
    assert [t.a1_range for t in reloaded.tables] == [t.a1_range for t in manifest.tables]
    assert reloaded.source.sha256 == manifest.source.sha256


def test_load_rejects_bad_dtype(tmp_path):
    bad = tmp_path / "bad.review.yaml"
    bad.write_text(
        "kind: hiveflow.spreadsheet-parser/review\nversion: 1\n"
        "source: {filename: x.xlsx, sha256: '0', bytes: 1, format: xlsx,"
        " parsed_at: '2026-01-01T00:00:00Z', parser_version: '0.1.0'}\n"
        "tables:\n"
        "- id: 'S!A1:B2'\n  name: t\n  sheet: S\n  a1_range: A1:B2\n  kind: table\n"
        "  confidence: 0.5\n  header_rows: 1\n"
        "  profile: {row_count: 1, column_count: 1}\n"
        "  columns:\n  - {source_name: a, name: a, ordinal: 0, dtype: MONEY}\n",
        encoding="utf-8",
    )
    with pytest.raises(ReviewError):
        load_review(bad)


def test_validate_flags_approved_without_columns(fx, fixtures_dir):
    manifest = draft_for_file(str(fixtures_dir / "clean_single.xlsx"))
    t = manifest.tables[0]
    t.approved = True
    for c in t.columns:
        c.include = False
    errors = validate_review(manifest)
    assert any("every column is excluded" in e for e in errors)


def test_apply_writes_one_csv_per_approved_table(fx, fixtures_dir, tmp_path):
    src = fixtures_dir / "two_stacked.xlsx"
    manifest = draft_for_file(str(src))
    manifest.tables[0].approved = True
    manifest.tables[0].name = "inventory"
    manifest.tables[1].approved = True
    manifest.tables[1].name = "pnl"

    assert validate_review(manifest) == []
    out = apply_review(manifest, read_workbook(str(src)), tmp_path / "out")

    names = sorted(p.name for p in (tmp_path / "out").glob("*.csv"))
    assert names == ["inventory.csv", "pnl.csv"]
    assert (tmp_path / "out" / "two_stacked.manifest.json").is_file()
    assert {t.name for t in out.tables} == {"inventory", "pnl"}
    assert all(t.row_count_out > 0 for t in out.tables)


def test_apply_skips_unapproved(fx, fixtures_dir, tmp_path):
    src = fixtures_dir / "two_stacked.xlsx"
    manifest = draft_for_file(str(src))
    manifest.tables[0].approved = True
    manifest.tables[0].name = "kept"
    # second table left unapproved
    out = apply_review(manifest, read_workbook(str(src)), tmp_path / "out")
    assert [t.name for t in out.tables] == ["kept"]
    assert any(s.reason == "not approved" for s in out.skipped)
