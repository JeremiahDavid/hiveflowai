"""Load client gold source-documentation catalogs from the lake bucket.

Reads governance/source_semantic_reference/{source}/gold/*.yaml produced by the
source-docs-gold Lambda (global catalogs + client overlays). Each connector
source has its own gold prefix and Semantic Reference.
"""

from __future__ import annotations

from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import read_yaml_artifact
from hiveflow.storage.paths import (
    governance_source_docs_gold_key,
    governance_source_semantic_latest_profile_key,
    governance_source_semantic_reference_prefix,
)

GOLD_ARTIFACTS: tuple[str, ...] = (
    "entity_properties",
    "entity_relationships",
    "entity_property_tags",
)

_FILENAMES: dict[str, str] = {
    "entity_properties": "entity_properties.yaml",
    "entity_relationships": "entity_relationships.yaml",
    "entity_property_tags": "entity_property_tags.yaml",
}

# Sources with a global docs → client gold merge pipeline today.
GOLD_BUILD_SOURCES: frozenset[str] = frozenset({"dbc"})


def normalize_reference_source(source: str) -> str:
    key = source.strip().lower()
    if key == "bc":
        return "dbc"
    return key


def source_docs_gold_key(settings: DnaSettings, artifact: str, *, source: str | None = None) -> str:
    name = _FILENAMES.get(artifact)
    if not name:
        raise ValueError(f"Unknown gold artifact {artifact!r}")
    connector = normalize_reference_source(source or settings.source)
    return governance_source_docs_gold_key(connector, name)


def load_silver_schema_profile(
    settings: DnaSettings,
    *,
    source: str | None = None,
) -> dict[str, Any] | None:
    connector = normalize_reference_source(source or settings.source)
    return read_yaml_artifact(
        settings,
        governance_source_semantic_latest_profile_key(connector),
    )


def load_source_docs_gold_artifact(
    settings: DnaSettings,
    artifact: str,
    *,
    source: str | None = None,
) -> dict[str, Any] | None:
    return read_yaml_artifact(
        settings,
        source_docs_gold_key(settings, artifact, source=source),
    )


def source_supports_gold_build(source: str) -> bool:
    return normalize_reference_source(source) in GOLD_BUILD_SOURCES


def list_reference_sources(
    settings: DnaSettings,
    *,
    configured: list[str] | None = None,
) -> list[str]:
    """Ordered connector sources that can appear in the Semantic Reference UI."""
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        key = normalize_reference_source(raw)
        if not key or key in seen:
            return
        seen.add(key)
        ordered.append(key)

    primary = normalize_reference_source(settings.source)
    if primary:
        _add(primary)
    for item in configured or []:
        _add(item)
    # Prefer known connector order after the primary source.
    for item in ("dbc", "qbo", "qbd"):
        if item in seen:
            continue
        # Only append known connectors when they were configured.
        if configured is None:
            continue
        if item in {normalize_reference_source(c) for c in configured}:
            _add(item)
    return ordered


def _artifact_has_silver_profile(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    merged = artifact.get("merged_from") or {}
    if not isinstance(merged, dict):
        return False
    profile = merged.get("silver_profile")
    return isinstance(profile, dict) and bool(profile.get("generated_at") or profile.get("s3"))


def load_source_docs_gold(settings: DnaSettings, *, source: str | None = None) -> dict[str, Any]:
    """Return all gold catalogs plus silver profile and presence flags for one connector."""
    connector = normalize_reference_source(source or settings.source)
    profile_key = governance_source_semantic_latest_profile_key(connector)
    silver_profile = load_silver_schema_profile(settings, source=connector)
    silver_profile_present = (
        isinstance(silver_profile, dict)
        and str(silver_profile.get("kind") or "") == "silver_schema_profile"
    )

    artifacts: dict[str, dict[str, Any] | None] = {}
    present: dict[str, bool] = {}
    for name in GOLD_ARTIFACTS:
        payload = load_source_docs_gold_artifact(settings, name, source=connector)
        artifacts[name] = payload
        present[name] = payload is not None

    any_present = any(present.values())
    all_present = all(present.values())
    properties = artifacts.get("entity_properties") or {}
    relationships = artifacts.get("entity_relationships") or {}
    tags = artifacts.get("entity_property_tags") or {}
    artifact_generated_at = {
        name: str((artifacts.get(name) or {}).get("generated_at") or "")
        for name in GOLD_ARTIFACTS
        if present.get(name)
    }

    silver_reconciled = silver_profile_present and all(
        _artifact_has_silver_profile(artifacts.get(name))
        for name in GOLD_ARTIFACTS
        if present.get(name)
    )

    in_silver_columns = 0
    doc_only_columns = 0
    for table in properties.get("tables") or properties.get("entities") or []:
        if not isinstance(table, dict):
            continue
        for prop in table.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            if prop.get("in_silver"):
                in_silver_columns += 1
            elif prop.get("in_silver") is False:
                doc_only_columns += 1

    profile_generated_at = str(silver_profile.get("generated_at") or "") if silver_profile_present else ""
    profile_consolidated_at = str(silver_profile.get("consolidated_at") or "") if silver_profile_present else ""

    return {
        "source": connector,
        "prefix": governance_source_semantic_reference_prefix(connector),
        "gold_prefix": f"{governance_source_semantic_reference_prefix(connector)}/gold",
        "silver_profile_key": profile_key,
        "silver_profile_present": silver_profile_present,
        "silver_profile": silver_profile if silver_profile_present else None,
        "silver_reconciled": silver_reconciled,
        "available": any_present,
        "complete": all_present,
        "build_supported": source_supports_gold_build(connector),
        "present": present,
        "entity_properties": properties if present["entity_properties"] else None,
        "entity_relationships": relationships if present["entity_relationships"] else None,
        "entity_property_tags": tags if present["entity_property_tags"] else None,
        "summary": {
            "table_count": int(
                properties.get("table_count")
                or properties.get("entity_count")
                or len(properties.get("tables") or properties.get("entities") or [])
            ),
            "property_count": int(properties.get("property_count") or 0),
            "relationship_count": int(relationships.get("relationship_count") or 0),
            "tagged_property_count": int(tags.get("tagged_property_count") or 0),
            "silver_profile_present": silver_profile_present,
            "silver_profile_generated_at": profile_generated_at,
            "silver_profile_consolidated_at": profile_consolidated_at,
            "silver_table_count": int(silver_profile.get("table_count") or 0) if silver_profile_present else 0,
            "silver_reconciled": silver_reconciled,
            "silver_column_count": in_silver_columns,
            "documentation_only_column_count": doc_only_columns,
            "generated_at": str(
                properties.get("generated_at")
                or relationships.get("generated_at")
                or tags.get("generated_at")
                or ""
            ),
            "artifact_generated_at": artifact_generated_at,
        },
    }
