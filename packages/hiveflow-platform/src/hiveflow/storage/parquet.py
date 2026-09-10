"""Shared Parquet / JSON lake I/O (platform)."""

from __future__ import annotations

import io
import json
from datetime import datetime

from hiveflow.compat import UTC
from hiveflow.storage.aws import s3_client
from pathlib import Path
from typing import Any, Protocol


class LakeDestination(Protocol):
    s3_bucket: str | None
    s3_prefix: str
    data_dir: Path


# Back-compat alias used by ingest connectors.
IngestDestination = LakeDestination


def run_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def local_run_dir(settings: LakeDestination, run_id: str | None = None) -> Path:
    from hiveflow.storage.paths import prefix_path

    return prefix_path(settings.data_dir, settings.s3_prefix, run_id or run_stamp())


def s3_run_prefix(settings: LakeDestination, run_id: str | None = None) -> str:
    prefix = settings.s3_prefix.strip("/")
    return f"{prefix}/{run_id or run_stamp()}"


def resolve_run_path(settings: LakeDestination, run_id: str | None = None) -> str | Path:
    if settings.s3_bucket:
        return s3_run_prefix(settings, run_id)
    return local_run_dir(settings, run_id)


def normalize_row_for_parquet(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize nested values so PyArrow can write stable Parquet schemas."""
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dict, list)):
            normalized[key] = json.dumps(value, default=str)
        else:
            normalized[key] = value
    return normalized


def rows_to_parquet_bytes(rows: list[dict[str, Any]]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    normalized_rows = [normalize_row_for_parquet(row) for row in rows]
    table = pa.Table.from_pylist(normalized_rows) if normalized_rows else pa.table({})
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    return buffer.getvalue()


def write_json_local(output_dir: Path, filename: str, payload: dict[str, Any]) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / filename
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(out_path)


def write_parquet_local(output_dir: Path, filename: str, rows: list[dict[str, Any]]) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / filename
    out_path.write_bytes(rows_to_parquet_bytes(rows))
    return str(out_path)


def read_json_local(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def read_json_s3(bucket: str, key: str) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    client = s3_client()
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    payload = json.loads(response["Body"].read().decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def write_json_s3(settings: LakeDestination, key: str, payload: dict[str, Any]) -> str:
    if not settings.s3_bucket:
        raise ValueError("s3_bucket is required for S3 writes")

    body = json.dumps(payload, indent=2).encode("utf-8")
    s3_client().put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    return f"s3://{settings.s3_bucket}/{key}"


def write_parquet_s3(settings: LakeDestination, key: str, rows: list[dict[str, Any]]) -> str:
    if not settings.s3_bucket:
        raise ValueError("s3_bucket is required for S3 writes")

    s3_client().put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=rows_to_parquet_bytes(rows),
        ContentType="application/vnd.apache.parquet",
    )
    return f"s3://{settings.s3_bucket}/{key}"


def read_parquet_local(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    if not path.is_file():
        return []
    table = pq.read_table(path)
    rows = table.to_pylist()
    return rows if isinstance(rows, list) else []


def read_parquet_s3(bucket: str, key: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq
    from botocore.exceptions import ClientError

    client = s3_client()
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            raise FileNotFoundError(key) from exc
        raise
    payload = response["Body"].read()
    table = pq.read_table(io.BytesIO(payload))
    rows = table.to_pylist()
    return rows if isinstance(rows, list) else []
