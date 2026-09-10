"""Silver entity discovery helpers for DNA Catalog."""

from __future__ import annotations

import io
from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import read_silver_entity
from hiveflow.storage.paths import prefix_path

_PREVIEW_LIMIT = 20


def list_silver_entities(settings: DnaSettings) -> list[str]:
    from hiveflow.entity_registry import catalog_entity_names

    source = settings.source.strip().lower()
    connector = source
    try:
        names = catalog_entity_names(connector, {})
    except (ValueError, ImportError):
        names = []
    return sorted({name.strip().lower() for name in names if name.strip()})


def _list_lake_layer_entities(settings: DnaSettings, source_prefix: str) -> list[str]:
    prefix = source_prefix.rstrip("/") + "/"
    names: set[str] = set()
    if settings.s3_bucket:
        from hiveflow.storage.aws import s3_client

        client = s3_client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=settings.s3_bucket,
            Prefix=prefix,
            Delimiter="/",
        ):
            for entry in page.get("CommonPrefixes") or []:
                entity = str(entry.get("Prefix") or "").strip("/").rsplit("/", 1)[-1]
                entity = entity.strip().lower()
                if entity and not entity.startswith("_"):
                    names.add(entity)
        return sorted(names)

    root = prefix_path(settings.data_dir, source_prefix)
    if not root.is_dir():
        return []
    for child in root.iterdir():
        if child.is_dir() and not child.name.startswith("_") and (child / "data.parquet").is_file():
            names.add(child.name.strip().lower())
    return sorted(names)


def list_lake_silver_stg_entities(settings: DnaSettings) -> list[str]:
    """Entity folders that already have ingest silver_stg parquet (local or S3)."""
    from hiveflow.storage.paths import silver_stg_source_prefix

    return _list_lake_layer_entities(settings, silver_stg_source_prefix(settings.source))


def list_lake_silver_entities(settings: DnaSettings, *, source: str | None = None) -> list[str]:
    """Entity folders that already have DNA silver parquet (local or S3)."""
    from hiveflow.storage.paths import silver_source_prefix

    return _list_lake_layer_entities(settings, silver_source_prefix(source or settings.source))


def list_lake_gold_outputs(settings: DnaSettings) -> list[str]:
    """Gold DNA output folders that already have parquet (local or S3)."""
    return _list_lake_layer_entities(settings, settings.gold_dna_prefix)


def _parquet_schema_columns(settings: DnaSettings, entity: str, *, layer: str = "silver") -> list[str]:
    import pyarrow.parquet as pq

    from hiveflow.storage.paths import (
        silver_entity_parquet_key,
        silver_entity_prefix,
        silver_stg_entity_parquet_key,
        silver_stg_entity_prefix,
    )

    entity_name = entity.strip().lower()
    if layer == "silver_stg":
        parquet_key = silver_stg_entity_parquet_key(settings.source, entity_name)
        local_prefix = silver_stg_entity_prefix(settings.source, entity_name)
    else:
        parquet_key = silver_entity_parquet_key(settings.source, entity_name)
        local_prefix = silver_entity_prefix(settings.source, entity_name)
    if settings.s3_bucket:
        from botocore.exceptions import ClientError

        from hiveflow.storage.aws import s3_client

        try:
            payload = s3_client().get_object(Bucket=settings.s3_bucket, Key=parquet_key)["Body"].read()
        except ClientError:
            return []
        schema = pq.read_schema(io.BytesIO(payload))
        return [str(field.name) for field in schema]
    path = prefix_path(
        settings.data_dir,
        local_prefix,
        "data.parquet",
    )
    if not path.is_file():
        return []
    schema = pq.read_schema(path)
    return [str(field.name) for field in schema]


def discover_silver_stg_columns(settings: DnaSettings, entity: str) -> list[str]:
    """Column names from ingest silver_stg parquet (not DNA silver)."""
    return _parquet_schema_columns(settings, entity, layer="silver_stg")


def discover_silver_columns(settings: DnaSettings, entity: str) -> list[str]:
    columns = _parquet_schema_columns(settings, entity, layer="silver")
    if columns:
        return columns
    columns = discover_silver_stg_columns(settings, entity)
    if columns:
        return columns
    rows = read_silver_entity(settings, entity)
    if not rows:
        return []
    keys: set[str] = set()
    for row in rows[:50]:
        if isinstance(row, dict):
            keys.update(row.keys())
    return sorted(keys)


def preview_silver_entity(
    settings: DnaSettings,
    entity: str,
    *,
    limit: int = _PREVIEW_LIMIT,
) -> list[dict[str, Any]]:
    rows = read_silver_entity(settings, entity)
    return rows[: max(1, limit)]
