"""Thin service layer over `hiveflow.dna.data_profile` for the portal UI.

No profiling/Bedrock logic lives here — every function below is a direct call
into the engine package, matching the Spreadsheet Engine's service.py
convention (business logic stays in the engine, the portal only composes and
persists a per-request result).
"""

from __future__ import annotations

from typing import Any

from hiveflow.dna.data_profile import (
    load_data_profile_index,
    load_entity_profile,
    profile_entity,
    refresh_data_profile_for_source,
    scoped_settings,
    update_profile_text,
)
from hiveflow.dna.field_semantics import list_lake_silver_stg_entities
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.source_docs.reference import list_reference_sources


def list_profile_rows(
    settings: DnaSettings, *, configured_sources: list[str] | None = None
) -> list[dict[str, Any]]:
    """One row per candidate table across every configured source, merging
    profiled tables (from the data-profile index) with unprofiled ones
    (discovered via schema introspection) so the index page can offer a
    "Profile now" action for anything not yet covered."""
    sources = list_reference_sources(settings, configured=configured_sources)
    index = load_data_profile_index(settings)
    profiled_by_key = {(str(t.get("source")), str(t.get("entity"))): t for t in index.get("tables") or []}

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        try:
            entities = list_lake_silver_stg_entities(scoped_settings(settings, source))
        except Exception:  # noqa: BLE001
            entities = []
        for entity in entities:
            key = (source, entity)
            if key in seen:
                continue
            seen.add(key)
            profiled = profiled_by_key.get(key)
            rows.append(
                {
                    "source": source,
                    "entity": entity,
                    "profiled": profiled is not None,
                    "purpose": str((profiled or {}).get("purpose") or ""),
                    "row_count": int((profiled or {}).get("row_count") or 0),
                    "field_count": int((profiled or {}).get("field_count") or 0),
                    "last_profiled_at": str((profiled or {}).get("last_profiled_at") or ""),
                }
            )

    # Anything in the index but no longer discoverable via schema introspection
    # (e.g. a table that was dropped) still shows up so nothing silently disappears.
    for (source, entity), profiled in profiled_by_key.items():
        if (source, entity) in seen:
            continue
        rows.append(
            {
                "source": source,
                "entity": entity,
                "profiled": True,
                "purpose": str(profiled.get("purpose") or ""),
                "row_count": int(profiled.get("row_count") or 0),
                "field_count": int(profiled.get("field_count") or 0),
                "last_profiled_at": str(profiled.get("last_profiled_at") or ""),
            }
        )

    rows.sort(key=lambda r: (r["source"], r["entity"]))
    return rows


def load_profile(settings: DnaSettings, source: str, entity: str) -> dict[str, Any] | None:
    return load_entity_profile(settings, source, entity)


def refresh_source(settings: DnaSettings, source: str) -> dict[str, Any]:
    """Re-run profiling (real Bedrock invoke) for every entity in one source."""
    return refresh_data_profile_for_source(settings, source)


def refresh_entity(settings: DnaSettings, source: str, entity: str) -> dict[str, Any]:
    """Re-run profiling (real Bedrock invoke) for a single entity — the core
    "test/refine the Bedrock output" action."""
    return profile_entity(settings, source, entity)


def save_overrides(
    settings: DnaSettings,
    source: str,
    entity: str,
    *,
    purpose: str | None = None,
    field_descriptions: dict[str, str] | None = None,
) -> dict[str, Any]:
    return update_profile_text(
        settings, source, entity, purpose=purpose, field_descriptions=field_descriptions
    )
