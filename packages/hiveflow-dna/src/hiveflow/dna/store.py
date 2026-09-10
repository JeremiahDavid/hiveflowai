from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.storage.aws import s3_client
from hiveflow.storage.paths import prefix_path


def read_silver_entity(settings: DnaSettings, entity_name: str) -> list[dict[str, Any]]:
    return _read_lake_entity(settings, entity_name, layer="silver")


def read_silver_stg_entity(settings: DnaSettings, entity_name: str) -> list[dict[str, Any]]:
    rows = _read_lake_entity(settings, entity_name, layer="silver_stg")
    if rows:
        return rows
    return _read_lake_entity(settings, entity_name, layer="silver")


def _read_lake_entity(settings: DnaSettings, entity_name: str, *, layer: str) -> list[dict[str, Any]]:
    from hiveflow.storage.paths import (
        legacy_silver_entity_parquet_key,
        silver_entity_parquet_key,
        silver_entity_prefix,
        silver_stg_entity_parquet_key,
        silver_stg_entity_prefix,
    )

    if layer == "silver_stg":
        parquet_key = silver_stg_entity_parquet_key(settings.source, entity_name)
        local_prefix = silver_stg_entity_prefix(settings.source, entity_name)
    else:
        parquet_key = silver_entity_parquet_key(settings.source, entity_name)
        local_prefix = silver_entity_prefix(settings.source, entity_name)

    if settings.s3_bucket:
        from hiveflow.storage.parquet import read_parquet_s3

        keys = [parquet_key]
        if layer == "silver":
            keys.append(legacy_silver_entity_parquet_key(settings.source, entity_name))
        for key in keys:
            try:
                return read_parquet_s3(settings.s3_bucket, key)
            except FileNotFoundError:
                continue
        return []
    from hiveflow.storage.parquet import read_parquet_local

    path = prefix_path(settings.data_dir, local_prefix, "data.parquet")
    return read_parquet_local(path)


def write_staging_output(
    settings: DnaSettings,
    output_id: str,
    rows: list[dict[str, Any]],
) -> str:
    from hiveflow.storage.parquet import write_parquet_local, write_parquet_s3

    if settings.s3_bucket:
        key = f"{settings.gold_dna_staging_prefix}/{output_id}/data.parquet"
        return write_parquet_s3(settings, key, rows)

    out_dir = prefix_path(settings.data_dir, settings.gold_dna_staging_prefix, output_id)
    return write_parquet_local(out_dir, "data.parquet", rows)


def read_staging_output(settings: DnaSettings, output_id: str) -> list[dict[str, Any]]:
    if settings.s3_bucket:
        from hiveflow.storage.parquet import read_parquet_s3

        key = f"{settings.gold_dna_staging_prefix}/{output_id}/data.parquet"
        try:
            return read_parquet_s3(settings.s3_bucket, key)
        except FileNotFoundError:
            return []
    from hiveflow.storage.parquet import read_parquet_local

    path = prefix_path(settings.data_dir, settings.gold_dna_staging_prefix, output_id, "data.parquet")
    return read_parquet_local(path)


def write_production_output(
    settings: DnaSettings,
    output_id: str,
    rows: list[dict[str, Any]],
) -> str:
    from hiveflow.storage.parquet import write_parquet_local, write_parquet_s3

    if settings.s3_bucket:
        key = f"{settings.gold_dna_prefix}/{output_id}/data.parquet"
        return write_parquet_s3(settings, key, rows)

    out_dir = prefix_path(settings.data_dir, settings.gold_dna_prefix, output_id)
    return write_parquet_local(out_dir, "data.parquet", rows)


def read_production_output(settings: DnaSettings, output_id: str) -> list[dict[str, Any]]:
    if settings.s3_bucket:
        from hiveflow.storage.parquet import read_parquet_s3

        key = f"{settings.gold_dna_prefix}/{output_id}/data.parquet"
        try:
            return read_parquet_s3(settings.s3_bucket, key)
        except FileNotFoundError:
            return []
    from hiveflow.storage.parquet import read_parquet_local

    path = prefix_path(settings.data_dir, settings.gold_dna_prefix, output_id, "data.parquet")
    return read_parquet_local(path)


def write_json_artifact(settings: DnaSettings, relative_key: str, payload: dict[str, Any]) -> str:
    if settings.s3_bucket:
        body = json.dumps(payload, indent=2).encode("utf-8")
        s3_client().put_object(
            Bucket=settings.s3_bucket,
            Key=relative_key,
            Body=body,
            ContentType="application/json",
        )
        return f"s3://{settings.s3_bucket}/{relative_key}"

    path = prefix_path(settings.data_dir, relative_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)


def write_text_artifact(
    settings: DnaSettings,
    relative_key: str,
    text: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> str:
    body = text.encode("utf-8")
    if settings.s3_bucket:
        s3_client().put_object(
            Bucket=settings.s3_bucket,
            Key=relative_key,
            Body=body,
            ContentType=content_type,
        )
        return f"s3://{settings.s3_bucket}/{relative_key}"

    path = prefix_path(settings.data_dir, relative_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


def read_text_artifact(settings: DnaSettings, relative_key: str) -> str | None:
    if settings.s3_bucket:
        from botocore.exceptions import ClientError

        client = s3_client()
        try:
            response = client.get_object(Bucket=settings.s3_bucket, Key=relative_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return response["Body"].read().decode("utf-8")

    path = prefix_path(settings.data_dir, relative_key)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def write_yaml_artifact(settings: DnaSettings, relative_key: str, payload: dict[str, Any]) -> str:
    import yaml

    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return write_text_artifact(
        settings,
        relative_key,
        text,
        content_type="application/yaml; charset=utf-8",
    )


def read_yaml_artifact(settings: DnaSettings, relative_key: str) -> dict[str, Any] | None:
    import yaml

    text = read_text_artifact(settings, relative_key)
    if text is None:
        return None
    payload = yaml.safe_load(text)
    return payload if isinstance(payload, dict) else None


def read_json_artifact(settings: DnaSettings, relative_key: str) -> dict[str, Any] | None:
    if settings.s3_bucket:
        from botocore.exceptions import ClientError

        client = s3_client()
        try:
            response = client.get_object(Bucket=settings.s3_bucket, Key=relative_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None

    path = prefix_path(settings.data_dir, relative_key)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def list_json_artifact_keys(settings: DnaSettings, prefix: str) -> list[str]:
    """List JSON artifact keys under a relative prefix (local dir or S3)."""
    normalized = prefix.strip().replace("\\", "/").lstrip("/")
    if normalized and not normalized.endswith("/"):
        normalized += "/"

    if settings.s3_bucket:
        client = s3_client()
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": settings.s3_bucket, "Prefix": normalized}
            if token:
                kwargs["ContinuationToken"] = token
            response = client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith(".json"):
                    keys.append(key)
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return sorted(keys)

    root = prefix_path(settings.data_dir, normalized)
    if not root.is_dir():
        return []
    return sorted(
        str(path.relative_to(settings.data_dir).as_posix())
        for path in root.rglob("*.json")
    )


def definition_pack_key(pack_id: str, version: str) -> str:
    """Legacy definition-pack key (pre-governance). Prefer governance_dna_key."""
    return f"dna/definition_packs/v{version}/{pack_id}.yaml"


def load_pack_from_settings(settings: DnaSettings) -> Any:
    from hiveflow.dna.governance import load_governance_dna, load_governance_workflow
    from hiveflow.dna.schema import load_definition_pack_file
    from hiveflow.dna.init_client import dna_boilerplate_path

    pack_id = settings.dna_config_id
    version = settings.pack_version
    if not version:
        workflow = load_governance_workflow(settings, pack_id) or {}
        version = workflow.get("active_version")
    if version:
        try:
            return load_governance_dna(settings, pack_id, str(version))
        except FileNotFoundError:
            pass

    # Last resort for local tooling only — not used once governance is seeded.
    boilerplate = dna_boilerplate_path()
    if boilerplate.is_file():
        pack = load_definition_pack_file(boilerplate)
        pack.pack_id = pack_id
        return pack
    raise FileNotFoundError(f"No company DNA config found for {pack_id!r}")
