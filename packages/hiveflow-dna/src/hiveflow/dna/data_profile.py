"""Per-client data profiling and description engine.

Samples silver_stg (or silver) parquet for any connector source, or the
Spreadsheet Engine's reference tables, profiles each column with the shared
primitives in `hiveflow.profiling`, and proposes a business purpose (per
entity) and description (per field) via Bedrock — falling back to a heuristic
when Bedrock is unavailable or returns invalid JSON, mirroring
`hiveflow.spreadsheet.interpret`'s resilience pattern.

Output is a durable per-client metadata document under
`governance/{pack_id}/data_profile/`, meant to be refreshed incrementally as
new data lands (a new connector sync, a newly approved spreadsheet table) —
this is the metadata layer future mapping/AI work reads from, rather than
raw schema introspection.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from hiveflow.compat import UTC
from typing import Any, Callable

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import (
    read_silver_entity,
    read_silver_stg_entity,
    read_yaml_artifact,
    write_yaml_artifact,
)
from hiveflow.profiling import profile_column
from hiveflow.storage.paths import (
    governance_data_profile_entity_key,
    governance_data_profile_index_key,
)

DEFAULT_SAMPLE_LIMIT = 200
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

InvokeFn = Callable[[str, str], str]

_SYSTEM_PROMPT = """You describe data tables for a business data platform.
Given a table name, row count, and per-column profiling stats (inferred type,
null rate, cardinality, sample values), return strict JSON only (no markdown):
{
  "purpose": "one sentence business purpose of this table",
  "confidence": 0.0-1.0,
  "fields": [
    {"name": "column_name", "description": "one sentence business meaning"}
  ]
}
Use the sample values and column names to infer business meaning. Prefer concise,
concrete descriptions a business analyst would recognize."""


def _default_invoke(system: str, user_message: str) -> str:
    import boto3
    from botocore.config import Config

    model_id = os.getenv("HIVEFLOW_BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID).strip()
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=30, read_timeout=120, retries={"max_attempts": 2}),
    )
    response = client.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
    )
    content = response.get("output", {}).get("message", {}).get("content") or []
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
    return "\n".join(parts).strip()


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = _JSON_FENCE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("Bedrock response must be a JSON object")
    return payload


def _heuristic_description(
    *, source: str, entity: str, columns: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "purpose": f"Data from {source}.{entity}",
        "confidence": 0.3,
        "fields": [{"name": col["name"], "description": f"Column {col['name']}"} for col in columns],
        "notes": ["Heuristic fallback — Bedrock unavailable or returned invalid JSON."],
    }


def scoped_settings(settings: DnaSettings, source: str) -> DnaSettings:
    """Settings for reading a lake entity that may live under a different source
    than `settings.source` (e.g. Spreadsheet Engine's `reference` tables)."""
    if source.strip().lower() == settings.source.strip().lower():
        return settings
    return DnaSettings(
        source=source,
        data_dir=settings.data_dir,
        s3_bucket=settings.s3_bucket,
        company=settings.company,
    )


def sample_entity_rows(
    settings: DnaSettings,
    source: str,
    entity: str,
    *,
    layer: str = "silver_stg",
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Sample up to `limit` rows from a lake entity (any source, silver or silver_stg)."""
    scoped = scoped_settings(settings, source)
    reader = read_silver_stg_entity if layer == "silver_stg" else read_silver_entity
    rows = reader(scoped, entity)
    return rows[: max(1, limit)]


def profile_entity(
    settings: DnaSettings,
    source: str,
    entity: str,
    *,
    layer: str = "silver_stg",
    limit: int = DEFAULT_SAMPLE_LIMIT,
    invoke: InvokeFn | None = None,
) -> dict[str, Any]:
    """Profile one lake entity and persist its metadata document.

    `invoke=False` skips Bedrock entirely (heuristic descriptions only — the
    existing test convention for AI-touching engines in this repo).
    """
    rows = sample_entity_rows(settings, source, entity, layer=layer, limit=limit)
    column_names = sorted({key for row in rows for key in row.keys()}) if rows else []
    columns = [profile_column(name, [row.get(name) for row in rows]) for name in column_names]

    description_payload: dict[str, Any] | None = None
    if invoke is not False:
        try:
            invoke_fn = invoke or _default_invoke
            user_payload = {
                "source": source,
                "entity": entity,
                "row_count": len(rows),
                "columns": [
                    {
                        "name": col["name"],
                        "inferred_type": col["inferred_type"],
                        "null_rate": col["null_rate"],
                        "cardinality": col["cardinality"],
                        "sample_values": col["sample_values"],
                    }
                    for col in columns
                ],
            }
            raw = invoke_fn(_SYSTEM_PROMPT, json.dumps(user_payload, default=str))
            description_payload = _extract_json(raw)
        except Exception:  # noqa: BLE001
            description_payload = None

    if not description_payload:
        description_payload = _heuristic_description(source=source, entity=entity, columns=columns)

    field_descriptions = {
        str(item.get("name") or ""): str(item.get("description") or "")
        for item in description_payload.get("fields") or []
        if isinstance(item, dict)
    }
    for col in columns:
        col["description"] = field_descriptions.get(col["name"], f"Column {col['name']}")

    profile = {
        "source": source,
        "entity": entity,
        "layer": layer,
        "row_count": len(rows),
        "profiled_at": datetime.now(UTC).isoformat(),
        "purpose": str(description_payload.get("purpose") or f"Data from {source}.{entity}"),
        "confidence": float(description_payload.get("confidence") or 0.3),
        "fields": columns,
        "notes": list(description_payload.get("notes") or []),
    }

    write_yaml_artifact(
        settings,
        governance_data_profile_entity_key(settings.dna_config_id, source, entity),
        profile,
    )
    _update_index(settings, profile)
    return profile


def _update_index(settings: DnaSettings, profile: dict[str, Any]) -> None:
    pack_id = settings.dna_config_id
    index_key = governance_data_profile_index_key(pack_id)
    index = read_yaml_artifact(settings, index_key) or {"pack_id": pack_id, "tables": []}
    tables = [
        t
        for t in index.get("tables") or []
        if not (t.get("source") == profile["source"] and t.get("entity") == profile["entity"])
    ]
    tables.append(
        {
            "source": profile["source"],
            "entity": profile["entity"],
            "layer": profile["layer"],
            "row_count": profile["row_count"],
            "field_count": len(profile["fields"]),
            "purpose": profile["purpose"],
            "last_profiled_at": profile["profiled_at"],
        }
    )
    tables.sort(key=lambda t: (str(t.get("source")), str(t.get("entity"))))
    index["tables"] = tables
    write_yaml_artifact(settings, index_key, index)


def load_data_profile_index(settings: DnaSettings) -> dict[str, Any]:
    pack_id = settings.dna_config_id
    return read_yaml_artifact(settings, governance_data_profile_index_key(pack_id)) or {
        "pack_id": pack_id,
        "tables": [],
    }


def load_entity_profile(settings: DnaSettings, source: str, entity: str) -> dict[str, Any] | None:
    pack_id = settings.dna_config_id
    return read_yaml_artifact(settings, governance_data_profile_entity_key(pack_id, source, entity))


def update_profile_text(
    settings: DnaSettings,
    source: str,
    entity: str,
    *,
    purpose: str | None = None,
    field_descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Manually override the purpose/field descriptions on an existing profile
    document, without re-sampling or re-invoking Bedrock. Marks edited text
    with a `*_edited` flag so the UI can distinguish a human override from raw
    Bedrock output — a later `profile_entity` re-run replaces both."""
    profile = load_entity_profile(settings, source, entity)
    if profile is None:
        raise ValueError(f"No profile exists yet for {source}.{entity} — profile it first.")

    if purpose is not None:
        profile["purpose"] = purpose
        profile["purpose_edited"] = True

    if field_descriptions:
        by_name = {str(col.get("name")): col for col in profile.get("fields") or []}
        for name, description in field_descriptions.items():
            col = by_name.get(name)
            if col is None:
                continue
            col["description"] = description
            col["description_edited"] = True

    write_yaml_artifact(
        settings,
        governance_data_profile_entity_key(settings.dna_config_id, source, entity),
        profile,
    )
    _update_index(settings, profile)
    return profile


def refresh_data_profile_for_source(
    settings: DnaSettings,
    source: str,
    *,
    entities: list[str] | None = None,
    layer: str = "silver_stg",
    limit: int = DEFAULT_SAMPLE_LIMIT,
    invoke: InvokeFn | None = None,
) -> dict[str, Any]:
    """Profile every entity for one source (or an explicit subset) and update the index.

    This is the callable a connector sync or a portal action invokes to run new
    data through the profiler — e.g. after ingest consolidate lands new
    silver_stg entities, or after a Spreadsheet Engine table is approved.
    """
    from hiveflow.dna.field_semantics import list_lake_silver_stg_entities

    scoped = scoped_settings(settings, source)
    target_entities = entities if entities is not None else list_lake_silver_stg_entities(scoped)
    profiled: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entity in target_entities:
        try:
            profiled.append(
                profile_entity(settings, source, entity, layer=layer, limit=limit, invoke=invoke)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"entity": entity, "error": str(exc)})
    return {
        "source": source,
        "profiled_count": len(profiled),
        "entities": [p["entity"] for p in profiled],
        "errors": errors,
    }
