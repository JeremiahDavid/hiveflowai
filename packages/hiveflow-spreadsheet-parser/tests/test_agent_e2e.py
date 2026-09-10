"""End-to-end against Amazon Bedrock. Skipped unless AWS credentials are present.

pytest -m bedrock
"""

from __future__ import annotations

import os

import pytest

from hiveflow_spreadsheet_parser.agent import propose
from hiveflow_spreadsheet_parser.readers import read_workbook
from hiveflow_spreadsheet_parser.review import apply_review, validate_review

pytestmark = pytest.mark.bedrock


def _no_aws() -> bool:
    return not (
        os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    )


skip_no_aws = pytest.mark.skipif(_no_aws(), reason="no AWS credentials in the environment")


@skip_no_aws
async def test_propose_finds_both_tables_on_a_messy_sheet(fixtures_dir):
    result = await propose(str(fixtures_dir / "two_stacked.xlsx"))
    assert result.used_agent
    assert len(result.manifest.tables) == 2
    for t in result.manifest.tables:
        assert t.name and t.name == t.name.lower()
        assert t.profile.row_count > 0


@skip_no_aws
async def test_propose_then_apply_writes_cleaned_csv(fixtures_dir, tmp_path):
    src = fixtures_dir / "formats.xlsx"
    result = await propose(str(src))
    manifest = result.manifest
    assert manifest.tables

    t = manifest.tables[0]
    t.approved = True
    t.dedupe_on = [c.name for c in t.columns if c.semantic == "id"][:1]
    assert validate_review(manifest) == []

    out = apply_review(manifest, read_workbook(str(src)), tmp_path / "out")
    assert out.tables
    csvs = list((tmp_path / "out").glob("*.csv"))
    assert len(csvs) == 1
    assert out.tables[0].row_count_out >= 1
