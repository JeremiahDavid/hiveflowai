"""Generic S3-or-local JSON/bytes storage, shared by Spreadsheet Lab.

The production Spreadsheet Engine (``hiveflow.spreadsheet.jobs``,
``hiveflow.spreadsheet.materialize``) hand-rolls this same S3-vs-local branching
independently in each module, each with its own copy of the pytest-safety guard
below. This module exists so new code writes it once.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hiveflow.storage.aws import s3_client
from hiveflow.storage.paths import prefix_path


@dataclass(frozen=True)
class BlobLocation:
    """Where blobs live for the current process: a bucket, or a local data dir."""

    bucket: str
    data_dir: Path


def resolve_blob_location(
    *,
    bucket_env: str = "HIVEFLOW_S3_BUCKET",
    data_dir_env: str = "HIVEFLOW_DATA_DIR",
    allow_test_s3_env: str = "HIVEFLOW_ALLOW_TEST_S3",
) -> BlobLocation:
    """Resolve the active storage backend from environment variables.

    A leftover ``HIVEFLOW_S3_BUCKET`` in the ambient shell must never let a test
    silently write real data to production S3 — ``PYTEST_CURRENT_TEST`` (set by
    pytest for the duration of every running test, regardless of how it was
    invoked) is checked directly rather than relying on an env-clearing fixture,
    which a package with its own ``[tool.pytest.ini_options]`` can bypass.
    """
    bucket = os.getenv(bucket_env, "").strip()
    if bucket and os.getenv("PYTEST_CURRENT_TEST") and not os.getenv(allow_test_s3_env):
        bucket = ""
    data_dir = Path(os.getenv(data_dir_env, "data")).resolve()
    return BlobLocation(bucket=bucket, data_dir=data_dir)


def write_json(loc: BlobLocation, key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    return write_bytes(loc, key, body, content_type="application/json")


def read_json(loc: BlobLocation, key: str) -> dict[str, Any] | None:
    body = read_bytes_or_none(loc, key)
    if body is None:
        return None
    payload = json.loads(body.decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def write_bytes(loc: BlobLocation, key: str, body: bytes, *, content_type: str) -> str:
    if loc.bucket:
        s3_client().put_object(
            Bucket=loc.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
        return f"s3://{loc.bucket}/{key}"
    path = prefix_path(loc.data_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


def read_bytes(loc: BlobLocation, key: str) -> bytes:
    if loc.bucket:
        response = s3_client().get_object(Bucket=loc.bucket, Key=key)
        return response["Body"].read()
    return prefix_path(loc.data_dir, key).read_bytes()


def read_bytes_or_none(loc: BlobLocation, key: str) -> bytes | None:
    if loc.bucket:
        client = s3_client()
        try:
            response = client.get_object(Bucket=loc.bucket, Key=key)
        except client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "NoSuchKey":
                return None
            raise
        return response["Body"].read()
    path = prefix_path(loc.data_dir, key)
    if not path.exists():
        return None
    return path.read_bytes()


def list_keys(loc: BlobLocation, prefix: str, *, suffix: str = ".json") -> list[str]:
    """Full storage keys (not just filenames) directly under ``prefix`` ending in ``suffix``."""
    if loc.bucket:
        client = s3_client()
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=loc.bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith(suffix):
                    keys.append(key)
        return sorted(keys)
    root = prefix_path(loc.data_dir, prefix)
    if not root.exists():
        return []
    base = prefix.rstrip("/")
    return sorted(f"{base}/{path.name}" for path in root.glob(f"*{suffix}"))


def delete_prefix(loc: BlobLocation, prefix: str, *, keep_keys: frozenset[str] = frozenset()) -> None:
    """Delete every object under ``prefix``, except any key in ``keep_keys``."""
    if loc.bucket:
        client = s3_client()
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=loc.bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key and key not in keep_keys:
                    keys.append(key)
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            client.delete_objects(
                Bucket=loc.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        return
    root = prefix_path(loc.data_dir, prefix)
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            continue
        key = f"{prefix.rstrip('/')}/{path.relative_to(root).as_posix()}"
        if key in keep_keys:
            continue
        path.unlink(missing_ok=True)
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
