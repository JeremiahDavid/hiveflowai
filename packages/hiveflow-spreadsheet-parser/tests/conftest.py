"""Shared fixtures. Regenerates the .xlsx samples if they are missing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _ensure_fixtures() -> None:
    expected = {
        "clean_single.xlsx",
        "two_stacked.xlsx",
        "report_export.xlsx",
        "alternating_blank.xlsx",
        "wrapped_records.xlsx",
        "merged_headers.xlsx",
        "pivot_matrix.xlsx",
        "side_by_side.xlsx",
        "formats.xlsx",
    }
    if expected.issubset({p.name for p in FIXTURES.glob("*.xlsx")}):
        return
    sys.path.insert(0, str(_SCRIPTS))
    import make_fixtures  # type: ignore[import-not-found]

    make_fixtures.main()


_ensure_fixtures()


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def fx(fixtures_dir: Path):
    from hiveflow_spreadsheet_parser.readers import read_workbook

    def _load(name: str):
        return read_workbook(fixtures_dir / name)

    return _load
