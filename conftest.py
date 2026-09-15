"""Workspace-wide test isolation.

Every package under packages/*/tests ends up in one pytest session (see
testpaths in pyproject.toml). hiveflow.spreadsheet.jobs and friends resolve
storage by checking HIVEFLOW_S3_BUCKET before ever looking at
HIVEFLOW_DATA_DIR, so a real bucket left over in the ambient shell (e.g. from
a prior `cdk deploy` or manual CLI run) silently redirects test writes to
production S3 no matter what an individual test sets HIVEFLOW_DATA_DIR to.
This autouse fixture makes that structurally impossible: it clears the real
storage-selection env vars before every test and repoints HIVEFLOW_DATA_DIR at
a throwaway per-test directory, so a test has to opt back into S3 explicitly
(and deliberately) rather than by accident.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _block_real_storage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVEFLOW_S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("HIVEFLOW_DATA_DIR", str(tmp_path))
