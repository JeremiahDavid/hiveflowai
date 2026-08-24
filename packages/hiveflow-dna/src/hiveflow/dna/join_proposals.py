"""Propose lake joins from a table's grain and keys onto silver and gold."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hiveflow.dna._text_match import column_lookup as _column_lookup
from hiveflow.dna._text_match import norm as _norm
from hiveflow.dna._text_match import stems as _stems
from hiveflow.dna._text_match import tokens as _tokens
from hiveflow.dna.settings import DnaSettings


@dataclass
class JoinTarget:
    layer: str
    name: str
    source: str
    primary_key: str = ""
    grain: str = ""
    columns: list[str] = field(default_factory=list)
    grain_columns: list[str] = field(default_factory=list)
    pack_entity_id: str = ""


@dataclass
class JoinCatalog:
    targets: list[JoinTarget] = field(default_factory=list)
    pack_joins: list[dict[str, str]] = field(default_factory=list)


def source_keys(table: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        raw = str(name or "").strip()
        key = _norm(raw)
        if not raw or key in seen:
            return
        seen.add(key)
        keys.append(raw)

    for col in table.get("schema") or []:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "").strip()
        if col.get("is_key") or col.get("is_foreign_key") or col.get("likely_key"):
            _add(name)
    for name in table.get("key_candidates") or []:
        _add(str(name))
    profiling = table.get("profiling") or {}
    if isinstance(profiling, dict):
        for col in profiling.get("columns") or []:
            if isinstance(col, dict) and col.get("likely_key"):
                _add(str(col.get("name") or ""))
        for name in profiling.get("key_candidates") or []:
            _add(str(name))
    if not keys:
        for col in table.get("schema") or []:
            if isinstance(col, dict):
                _add(str(col.get("name") or ""))
    return keys


def _source_columns(table: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for col in table.get("schema") or []:
        if isinstance(col, dict) and str(col.get("name") or "").strip():
            names.append(str(col.get("name")).strip())
    if names:
        return names
    goal = table.get("clean_goal") or {}
    if isinstance(goal, dict):
        return [str(h).strip() for h in (goal.get("headers") or []) if str(h).strip()]
    return []


def _score_match(*, grain_overlap: int, exact_key: bool, stem_key: bool, gold_grain: bool) -> float:
    score = 0.15
    if exact_key:
        score += 0.55
    elif stem_key:
        score += 0.35
    if grain_overlap:
        score += min(0.25, 0.1 * grain_overlap)
    if gold_grain:
        score += 0.15
    return round(min(score, 0.99), 2)


def _match_keys(
    source_key_names: list[str],
    target: JoinTarget,
) -> tuple[str, str, bool, bool]:
    target_names = list(target.columns)
    if target.primary_key:
        target_names = [target.primary_key, *target_names]
    target_names.extend(target.grain_columns)
    lookup = _column_lookup(target_names)
    for source_key in source_key_names:
        exact = lookup.get(_norm(source_key))
        if exact and _norm(exact) == _norm(source_key):
            right = target.primary_key or exact
            return source_key, right, True, True
        for stem in _stems(source_key):
            hit = lookup.get(stem)
            if hit:
                right = target.primary_key if _norm(target.primary_key) in {"id", stem} else hit
                if not right:
                    right = hit
                return source_key, right, False, True
            if stem in _tokens(target.name) or stem in _tokens(target.grain):
                right = target.primary_key or "id"
                return source_key, right, False, True
            name_tokens = _tokens(target.name) | _tokens(target.grain)
            if f"{stem}s" in name_tokens or (stem.endswith("s") and stem[:-1] in name_tokens):
                right = target.primary_key or "id"
                return source_key, right, False, True
    return "", "", False, False


def _proposal_id(layer: str, source: str, name: str, left_key: str, right_key: str) -> str:
    return f"{layer}:{source}:{name}:{_norm(left_key)}:{_norm(right_key)}"


def propose_joins_from_catalog(
    table: dict[str, Any],
    catalog: JoinCatalog,
) -> dict[str, Any]:
    entity = str(table.get("entity_name") or table.get("table_id") or "spreadsheet_table").strip()
    grain = str(table.get("grain") or (table.get("clean_goal") or {}).get("grain") or "")
    grain_tokens = _tokens(grain) | _tokens(entity)
    keys = source_keys(table)
    if not keys:
        keys = _source_columns(table)
    skip_names = {_norm(entity)}
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()

    for target in catalog.targets:
        if _norm(target.name) in skip_names and target.layer != "gold":
            continue
        left_key, right_key, exact, stem = _match_keys(keys, target)
        overlap = len(grain_tokens & (_tokens(target.grain) | _tokens(target.name) | _tokens(target.primary_key)))
        gold_hit = False
        if target.layer == "gold" and target.grain_columns:
            gold_lookup = _column_lookup(target.grain_columns)
            for key in keys:
                if _norm(key) in gold_lookup or any(stem in gold_lookup for stem in _stems(key)):
                    gold_hit = True
                    if not left_key:
                        left_key = key
                        right_key = gold_lookup.get(_norm(key)) or gold_lookup.get(next(iter(_stems(key)), "")) or (
                            target.grain_columns[0]
                        )
                    break
        if not (exact or stem or gold_hit):
            continue
        if not left_key or not right_key:
            continue
        pid = _proposal_id(target.layer, target.source, target.name, left_key, right_key)
        if pid in seen:
            continue
        seen.add(pid)
        reasons = []
        if overlap:
            reasons.append(f"grain overlap ({', '.join(sorted(grain_tokens & (_tokens(target.grain) | _tokens(target.name))) or grain_tokens)})")
        if exact:
            reasons.append(f"key {left_key} matches {target.name}.{right_key}")
        elif stem:
            reasons.append(f"key stem of {left_key} matches {target.name}.{right_key}")
        if gold_hit:
            reasons.append("gold grain columns include this key")
        proposals.append(
            {
                "id": pid,
                "layer": target.layer,
                "target": target.name,
                "target_source": target.source,
                "left_table": entity,
                "left_key": left_key,
                "right_table": target.name,
                "right_key": right_key,
                "cardinality": "many_to_one",
                "confidence": _score_match(
                    grain_overlap=overlap,
                    exact_key=exact,
                    stem_key=stem,
                    gold_grain=gold_hit,
                ),
                "match_reason": "; ".join(reasons) or "key/grain match",
                "grain": target.grain,
                "grain_columns": list(target.grain_columns),
                "via_pack_join": "",
            }
        )

    by_id = {t.pack_entity_id: t for t in catalog.targets if t.pack_entity_id}
    matched_pack = [
        t
        for t in catalog.targets
        if t.pack_entity_id and any(p["target"] == t.name and p["layer"] == "silver" for p in proposals)
    ]
    for join in catalog.pack_joins:
        left = by_id.get(str(join.get("left_entity") or ""))
        right = by_id.get(str(join.get("right_entity") or ""))
        if not left or not right:
            continue
        for anchor in matched_pack:
            other = right if anchor.pack_entity_id == left.pack_entity_id else left if anchor.pack_entity_id == right.pack_entity_id else None
            if other is None:
                continue
            left_key = str(join.get("left_key") or "")
            right_key = str(join.get("right_key") or "")
            if anchor.pack_entity_id == left.pack_entity_id:
                src_key, dst_key = left_key, right_key
                dest = right
            else:
                src_key, dst_key = right_key, left_key
                dest = left
            dest_existing = next(
                (item for item in proposals if item.get("layer") == "silver" and item.get("target") == dest.name),
                None,
            )
            if dest_existing is not None:
                dest_existing["via_pack_join"] = str(join.get("id") or dest_existing.get("via_pack_join") or "")
                extra = f"DNA pack join {join.get('id')} via {anchor.name}"
                reason = str(dest_existing.get("match_reason") or "")
                if extra not in reason:
                    dest_existing["match_reason"] = f"{reason}; {extra}" if reason else extra
                continue
            pid = _proposal_id("silver", dest.source, dest.name, src_key, dst_key)
            if pid in seen:
                continue
            seen.add(pid)
            proposals.append(
                {
                    "id": pid,
                    "layer": "silver",
                    "target": dest.name,
                    "target_source": dest.source,
                    "left_table": entity,
                    "left_key": src_key,
                    "right_table": dest.name,
                    "right_key": dst_key,
                    "cardinality": str(join.get("cardinality") or "many_to_one"),
                    "confidence": 0.7,
                    "match_reason": (
                        f"DNA pack join {join.get('id')} via {anchor.name} "
                        f"({src_key} → {dest.name}.{dst_key})"
                    ),
                    "grain": dest.grain,
                    "grain_columns": list(dest.grain_columns),
                    "via_pack_join": str(join.get("id") or ""),
                }
            )

    proposals.sort(key=lambda item: (-float(item.get("confidence") or 0), item.get("target") or ""))
    notes = []
    if not proposals:
        notes.append("No silver or gold join targets matched this table's grain and keys.")
    return {
        "kind": "dna_join_proposals",
        "source_entity": entity,
        "source_grain": grain,
        "source_keys": keys,
        "proposals": proposals[:24],
        "notes": notes,
    }


def _gold_columns(settings: DnaSettings, output_id: str) -> list[str]:
    import io

    import pyarrow.parquet as pq

    from hiveflow.storage.paths import gold_dna_entity_parquet_key, prefix_path

    key = gold_dna_entity_parquet_key(output_id)
    if settings.s3_bucket:
        import boto3
        from botocore.exceptions import ClientError

        try:
            payload = boto3.client("s3").get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
        except ClientError:
            return []
        return [str(field.name) for field in pq.read_schema(io.BytesIO(payload))]
    path = prefix_path(settings.data_dir, key)
    if not path.is_file():
        return []
    return [str(field.name) for field in pq.read_schema(path)]


def build_join_catalog(settings: DnaSettings) -> JoinCatalog:
    from hiveflow.dna.field_semantics import (
        discover_silver_columns,
        list_lake_gold_outputs,
        list_lake_silver_entities,
    )
    from hiveflow.storage.paths import SPREADSHEET_REFERENCE_SOURCE

    catalog = JoinCatalog()
    seen: set[tuple[str, str, str]] = set()

    def _add(target: JoinTarget) -> None:
        key = (target.layer, target.source, _norm(target.name))
        if key in seen:
            return
        seen.add(key)
        catalog.targets.append(target)

    pack = None
    try:
        from hiveflow.dna.workflow import load_production_pack

        pack = load_production_pack(settings)
    except Exception:  # noqa: BLE001
        pack = None

    if pack is not None:
        for entity in pack.entities:
            columns = []
            try:
                columns = discover_silver_columns(settings, entity.silver_entity)
            except Exception:  # noqa: BLE001
                columns = []
            _add(
                JoinTarget(
                    layer="silver",
                    name=str(entity.silver_entity),
                    source=str(settings.source),
                    primary_key=str(entity.primary_key or "id"),
                    grain=str(entity.grain or ""),
                    columns=columns,
                    pack_entity_id=str(entity.id),
                )
            )
        for join in pack.joins:
            catalog.pack_joins.append(
                {
                    "id": str(join.id),
                    "left_entity": str(join.left_entity),
                    "right_entity": str(join.right_entity),
                    "left_key": str(join.left_key),
                    "right_key": str(join.right_key),
                    "cardinality": str(join.cardinality),
                }
            )
        for output in pack.outputs:
            grain_cols = list(output.columns or [])
            _add(
                JoinTarget(
                    layer="gold",
                    name=str(output.id),
                    source="dna",
                    grain=str(output.output_type or "gold"),
                    columns=grain_cols,
                    grain_columns=grain_cols,
                )
            )

    sql_pack = None
    try:
        from hiveflow.dna.sql_pack import load_sql_pack

        sql_pack = load_sql_pack(settings)
    except Exception:  # noqa: BLE001
        sql_pack = None
    if sql_pack is not None:
        for transform in sql_pack.by_layer("gold"):
            name = str(transform.output_id or transform.id)
            _add(
                JoinTarget(
                    layer="gold",
                    name=name,
                    source="dna",
                    grain="gold",
                    grain_columns=list(transform.grain_columns or []),
                    columns=list(transform.grain_columns or []),
                )
            )

    try:
        for name in list_lake_silver_entities(settings):
            columns = discover_silver_columns(settings, name)
            pk = "id" if any(_norm(col) == "id" for col in columns) else (columns[0] if columns else "id")
            _add(
                JoinTarget(
                    layer="silver",
                    name=name,
                    source=str(settings.source),
                    primary_key=pk,
                    grain=name,
                    columns=columns,
                )
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        for name in list_lake_silver_entities(settings, source=SPREADSHEET_REFERENCE_SOURCE):
            ref_settings = DnaSettings(
                source=SPREADSHEET_REFERENCE_SOURCE,
                data_dir=settings.data_dir,
                s3_bucket=settings.s3_bucket,
                company=settings.company,
            )
            columns = discover_silver_columns(ref_settings, name)
            pk = "id" if any(_norm(col) == "id" for col in columns) else (columns[0] if columns else "")
            _add(
                JoinTarget(
                    layer="silver",
                    name=name,
                    source=SPREADSHEET_REFERENCE_SOURCE,
                    primary_key=pk,
                    grain=name,
                    columns=columns,
                )
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        for name in list_lake_gold_outputs(settings):
            columns = _gold_columns(settings, name)
            _add(
                JoinTarget(
                    layer="gold",
                    name=name,
                    source="dna",
                    grain="gold",
                    columns=columns,
                    grain_columns=columns,
                )
            )
    except Exception:  # noqa: BLE001
        pass

    return catalog


def propose_joins_for_table(
    table: dict[str, Any],
    *,
    settings: DnaSettings | None = None,
    catalog: JoinCatalog | None = None,
) -> dict[str, Any]:
    """Propose silver and gold joins for one spreadsheet (or other) table."""
    if catalog is None:
        if settings is None:
            raise ValueError("settings or catalog is required")
        catalog = build_join_catalog(settings)
    return propose_joins_from_catalog(table, catalog)
