from __future__ import annotations

from pathlib import Path

DATA_LAYERS = ("raw", "silver_stg", "silver", "gold")


def layer_source_prefix(layer: str, source: str) -> str:
    """Build `{layer}/{source}` prefix inside the company data bucket."""
    layer_slug = layer.strip().strip("/").lower()
    source_slug = source.strip().strip("/").lower()
    if layer_slug not in DATA_LAYERS:
        raise ValueError(f"Unknown data layer {layer!r}. Expected one of: {', '.join(DATA_LAYERS)}")
    if not source_slug:
        raise ValueError("source is required for source-scoped layers")
    return f"{layer_slug}/{source_slug}"


def raw_source_prefix(source: str) -> str:
    return layer_source_prefix("raw", source)


def silver_stg_source_prefix(source: str) -> str:
    """Ingest consolidate prefix (pre-DNA)."""
    return layer_source_prefix("silver_stg", source)


def silver_source_prefix(source: str) -> str:
    """DNA-pack silver prefix (enhanced entities and gold sources)."""
    return layer_source_prefix("silver", source)


def gold_prefix() -> str:
    return "gold"


def gold_dna_prefix() -> str:
    return "gold/dna"


def gold_dna_staging_prefix() -> str:
    return "gold/dna/_staging"


def gold_dna_entity_prefix(output_id: str) -> str:
    return f"{gold_dna_prefix()}/{output_id.strip().lower()}"


def gold_dna_entity_parquet_key(output_id: str) -> str:
    return f"{gold_dna_entity_prefix(output_id)}/{SILVER_ENTITY_FILENAME}"


def dna_definition_pack_prefix(pack_id: str, version: str) -> str:
    """Legacy path — prefer governance_* helpers for new writes."""
    return f"dna/definition_packs/v{version.strip()}/{pack_id.strip().lower()}"


def company_slug(company: str) -> str:
    """Normalize company name for governance pack ids (e.g. POC → poc)."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", company.strip().lower()).strip("_")
    if not slug or not slug[0].isalpha():
        raise ValueError(f"company must start with a letter after normalization: {company!r}")
    return slug


def company_dna_config_id(company: str) -> str:
    """Canonical DNA pack id / filename stem: ``{company}_dna_config``."""
    return f"{company_slug(company)}_dna_config"


def company_reporting_config_id(company: str) -> str:
    """Canonical reporting pack id / filename stem: ``{company}_reporting_config``."""
    return f"{company_slug(company)}_reporting_config"


def governance_prefix() -> str:
    return "governance"


def governance_pack_prefix(pack_id: str) -> str:
    return f"{governance_prefix()}/{pack_id.strip().lower()}"


def governance_version_prefix(pack_id: str, version: str) -> str:
    return f"{governance_pack_prefix(pack_id)}/v{version.strip()}"


def governance_workflow_key(pack_id: str) -> str:
    return f"{governance_pack_prefix(pack_id)}/workflow.json"


def governance_manifest_key(pack_id: str, version: str) -> str:
    return f"{governance_version_prefix(pack_id, version)}/manifest.json"


def governance_dna_key(pack_id: str, version: str) -> str:
    """Versioned company DNA config YAML, e.g. ``governance/poc_dna_config/v1.0.0/poc_dna_config.yaml``."""
    pack = pack_id.strip().lower()
    return f"{governance_version_prefix(pack, version)}/{pack}.yaml"


def governance_dna_legacy_json_key(pack_id: str, version: str) -> str:
    """Pre-YAML governance DNA key (``dna.json``)."""
    return f"{governance_version_prefix(pack_id, version)}/dna.json"


def governance_reporting_key(pack_id: str, version: str, *, company: str | None = None) -> str:
    """Versioned reporting config YAML beside the DNA pack."""
    if company:
        name = company_reporting_config_id(company)
    elif pack_id.strip().lower().endswith("_dna_config"):
        name = pack_id.strip().lower()[: -len("_dna_config")] + "_reporting_config"
    else:
        name = "reporting"
    return f"{governance_version_prefix(pack_id, version)}/{name}.yaml"


def governance_reporting_legacy_json_key(pack_id: str, version: str) -> str:
    return f"{governance_version_prefix(pack_id, version)}/reporting.json"


def governance_docs_prefix(pack_id: str, version: str) -> str:
    return f"{governance_version_prefix(pack_id, version)}/docs"


def governance_sql_prefix(pack_id: str, version: str) -> str:
    """Pinned Athena SQL pack under a DNA governance semver."""
    return f"{governance_version_prefix(pack_id, version)}/sql"


def governance_sql_manifest_key(pack_id: str, version: str) -> str:
    return f"{governance_sql_prefix(pack_id, version)}/manifest.yaml"


def governance_sql_file_key(pack_id: str, version: str, relative_path: str) -> str:
    """Key for a SQL file relative to ``sql/`` (e.g. ``silver/add_col__x.sql``)."""
    name = relative_path.strip().lstrip("/").replace("\\", "/")
    if not name or name.endswith("/") or ".." in name.split("/"):
        raise ValueError(f"Invalid SQL relative path: {relative_path!r}")
    if not name.lower().endswith(".sql"):
        raise ValueError(f"SQL file must end with .sql: {relative_path!r}")
    return f"{governance_sql_prefix(pack_id, version)}/{name}"


def gold_dna_sql_staging_prefix(transform_id: str) -> str:
    slug = transform_id.strip().lower().replace(" ", "_")
    return f"{gold_dna_staging_prefix()}/_sql/{slug}"


def silver_sql_staging_prefix(source: str, entity: str, transform_id: str) -> str:
    slug = transform_id.strip().lower().replace(" ", "_")
    return f"{silver_entity_prefix(source, entity)}/_sql_staging/{slug}"


def silver_stg_sql_staging_prefix(source: str, entity: str, transform_id: str) -> str:
    slug = transform_id.strip().lower().replace(" ", "_")
    return f"{silver_stg_entity_prefix(source, entity)}/_sql_staging/{slug}"


def governance_proposals_prefix(pack_id: str) -> str:
    return f"{governance_pack_prefix(pack_id)}/proposals"


def governance_proposal_prefix(pack_id: str, proposal_id: str) -> str:
    pid = proposal_id.strip().lower()
    if not pid or ".." in pid or "/" in pid or "\\" in pid:
        raise ValueError(f"Invalid proposal id: {proposal_id!r}")
    return f"{governance_proposals_prefix(pack_id)}/{pid}"


def governance_proposal_meta_key(pack_id: str, proposal_id: str) -> str:
    return f"{governance_proposal_prefix(pack_id, proposal_id)}/meta.json"


def governance_proposal_dna_key(pack_id: str, proposal_id: str) -> str:
    return f"{governance_proposal_prefix(pack_id, proposal_id)}/dna.yaml"


def governance_proposal_reporting_key(pack_id: str, proposal_id: str) -> str:
    return f"{governance_proposal_prefix(pack_id, proposal_id)}/reporting.yaml"


def governance_proposal_conversation_key(pack_id: str, proposal_id: str) -> str:
    return f"{governance_proposal_prefix(pack_id, proposal_id)}/conversation.json"


def governance_doc_key(pack_id: str, version: str, filename: str) -> str:
    name = filename.strip().lstrip("/").replace("\\", "/")
    if not name or ".." in name.split("/"):
        raise ValueError(f"Invalid governance doc filename: {filename!r}")
    return f"{governance_docs_prefix(pack_id, version)}/{name}"


def governance_field_semantics_prefix(pack_id: str) -> str:
    return f"{governance_pack_prefix(pack_id)}/field_semantics"


def governance_field_semantics_draft_key(pack_id: str) -> str:
    return f"{governance_field_semantics_prefix(pack_id)}/draft.yaml"


def governance_field_semantics_workflow_key(pack_id: str) -> str:
    return f"{governance_field_semantics_prefix(pack_id)}/workflow.json"


def governance_field_semantics_version_prefix(pack_id: str, version: str) -> str:
    return f"{governance_field_semantics_prefix(pack_id)}/v{version.strip()}"


def governance_field_semantics_key(pack_id: str, version: str) -> str:
    return f"{governance_field_semantics_version_prefix(pack_id, version)}/field_semantics.yaml"


def governance_field_semantics_manifest_key(pack_id: str, version: str) -> str:
    return f"{governance_field_semantics_version_prefix(pack_id, version)}/manifest.json"


def governance_semantic_model_prefix(pack_id: str) -> str:
    return f"{governance_pack_prefix(pack_id)}/semantic_model"


def governance_semantic_model_draft_key(pack_id: str) -> str:
    return f"{governance_semantic_model_prefix(pack_id)}/draft.yaml"


def governance_semantic_model_workflow_key(pack_id: str) -> str:
    return f"{governance_semantic_model_prefix(pack_id)}/workflow.json"


def governance_semantic_model_version_prefix(pack_id: str, version: str) -> str:
    return f"{governance_semantic_model_prefix(pack_id)}/v{version.strip()}"


def governance_semantic_model_key(pack_id: str, version: str) -> str:
    return f"{governance_semantic_model_version_prefix(pack_id, version)}/semantic_model.yaml"


def governance_semantic_model_manifest_key(pack_id: str, version: str) -> str:
    return f"{governance_semantic_model_version_prefix(pack_id, version)}/manifest.json"


def governance_model_mapping_key(pack_id: str) -> str:
    """Per-client industry template mapping — single mutable document."""
    return f"{governance_pack_prefix(pack_id)}/model_mapping.yaml"


def governance_data_profile_prefix(pack_id: str) -> str:
    """Per-client data profiling / description metadata (all sources, all entities)."""
    return f"{governance_pack_prefix(pack_id)}/data_profile"


def governance_data_profile_entity_key(pack_id: str, source: str, entity: str) -> str:
    src = source.strip().lower()
    ent = entity.strip().lower()
    if not src or not ent:
        raise ValueError("source and entity are required")
    return f"{governance_data_profile_prefix(pack_id)}/{src}/{ent}.yaml"


def governance_data_profile_index_key(pack_id: str) -> str:
    return f"{governance_data_profile_prefix(pack_id)}/index.yaml"


def governance_semantic_docs_prefix(pack_id: str) -> str:
    """Tenant-uploaded markdown references for semantic RAG (optional)."""
    return f"{governance_pack_prefix(pack_id)}/semantic_docs"


def governance_semantic_overrides_key(pack_id: str) -> str:
    """Tenant YAML overrides for semantic entities, joins, and column hints."""
    return f"{governance_pack_prefix(pack_id)}/semantic_overrides.yaml"


def governance_source_semantic_reference_prefix(source: str) -> str:
    """Cross-tenant approved semantic build snapshots for one silver source connector."""
    connector = source.strip().lower()
    if not connector:
        raise ValueError("source is required")
    return f"{governance_prefix()}/source_semantic_reference/{connector}"


def governance_source_semantic_reference_index_key(source: str) -> str:
    return f"{governance_source_semantic_reference_prefix(source)}/index.json"


def governance_source_semantic_reference_consensus_key(source: str) -> str:
    return f"{governance_source_semantic_reference_prefix(source)}/consensus.yaml"


def governance_source_semantic_latest_profile_key(source: str) -> str:
    """Merged documentation + approved-build baseline used for silver profiling."""
    return f"{governance_source_semantic_reference_prefix(source)}/latest_profile.yaml"


def governance_source_semantic_reference_build_key(source: str, pack_id: str, version: str) -> str:
    pack = pack_id.strip().lower()
    ver = version.strip().replace("/", "_")
    if not pack or not ver:
        raise ValueError("pack_id and version are required")
    return f"{governance_source_semantic_reference_prefix(source)}/builds/{pack}__v{ver}.yaml"


def governance_source_docs_overlay_key(source: str, filename: str) -> str:
    """Client overlay YAML under governance/source_semantic_reference/{source}/."""
    name = filename.strip().lstrip("/")
    if not name:
        raise ValueError("filename is required")
    return f"{governance_source_semantic_reference_prefix(source)}/{name}"


def governance_source_docs_gold_prefix(source: str) -> str:
    """Merged global+client source docs (gold) for one connector."""
    return f"{governance_source_semantic_reference_prefix(source)}/gold"


def governance_source_docs_gold_key(source: str, filename: str) -> str:
    name = filename.strip().lstrip("/")
    if not name:
        raise ValueError("filename is required")
    return f"{governance_source_docs_gold_prefix(source)}/{name}"


def governance_source_docs_versions_prefix(source: str) -> str:
    """Version snapshots of overlays + gold for one connector source."""
    return f"{governance_source_semantic_reference_prefix(source)}/versions"


def governance_source_docs_versions_manifest_key(source: str) -> str:
    return f"{governance_source_docs_versions_prefix(source)}/manifest.yaml"


def governance_source_docs_version_prefix(source: str, version: int | str) -> str:
    ver = str(version).strip().lstrip("v")
    if not ver.isdigit():
        raise ValueError(f"version must be a positive integer, got {version!r}")
    return f"{governance_source_docs_versions_prefix(source)}/v{int(ver)}"


def governance_source_docs_version_overlay_key(
    source: str, version: int | str, filename: str
) -> str:
    name = filename.strip().lstrip("/")
    if not name:
        raise ValueError("filename is required")
    return f"{governance_source_docs_version_prefix(source, version)}/overlays/{name}"


def governance_source_docs_version_gold_key(
    source: str, version: int | str, filename: str
) -> str:
    name = filename.strip().lstrip("/")
    if not name:
        raise ValueError("filename is required")
    return f"{governance_source_docs_version_prefix(source, version)}/gold/{name}"


SILVER_ENTITY_FILENAME = "data.parquet"

# Spreadsheet Engine approved tables land in silver under this source prefix.
SPREADSHEET_REFERENCE_SOURCE = "reference"


def silver_stg_entity_prefix(source: str, entity: str) -> str:
    return f"{silver_stg_source_prefix(source)}/{entity.strip().lower()}"


def silver_stg_entity_parquet_key(source: str, entity: str) -> str:
    return f"{silver_stg_entity_prefix(source, entity)}/{SILVER_ENTITY_FILENAME}"


def silver_entity_prefix(source: str, entity: str) -> str:
    return f"{silver_source_prefix(source)}/{entity.strip().lower()}"


def silver_entity_parquet_key(source: str, entity: str) -> str:
    return f"{silver_entity_prefix(source, entity)}/{SILVER_ENTITY_FILENAME}"


def spreadsheet_reference_silver_entity_parquet_key(entity: str) -> str:
    """Parquet key for an approved Spreadsheet Engine reference table."""
    return silver_entity_parquet_key(SPREADSHEET_REFERENCE_SOURCE, entity)


def silver_baseline_fingerprint_key(source: str, entity: str) -> str:
    """Post-consolidate, pre-enhancement integrity baseline under silver_stg."""
    return f"{silver_stg_entity_prefix(source, entity)}/_baseline_fingerprint.json"


def legacy_silver_entity_parquet_key(source: str, entity: str) -> str:
    """Pre-directory silver key (also used as a cutover fallback from the old ingest prefix)."""
    return f"{silver_source_prefix(source)}/{entity.strip().lower()}.parquet"


def raw_entity_run_prefix(source: str, run_id: str, entity: str) -> str:
    return f"{raw_source_prefix(source)}/{run_id.strip()}/{entity.strip().lower()}"


def raw_entity_parquet_key(source: str, run_id: str, entity: str) -> str:
    return f"{raw_entity_run_prefix(source, run_id, entity)}/{SILVER_ENTITY_FILENAME}"


def legacy_raw_entity_parquet_key(source: str, run_id: str, entity: str) -> str:
    return f"{raw_source_prefix(source)}/{run_id.strip()}/{entity.strip().lower()}.parquet"


def spreadsheet_engine_prefix() -> str:
    """Governance artifacts for Spreadsheet Engine workbook analysis jobs."""
    return f"{governance_prefix()}/spreadsheet_engine"


def spreadsheet_engine_jobs_prefix() -> str:
    return f"{spreadsheet_engine_prefix()}/jobs"


def spreadsheet_engine_job_prefix(job_id: str) -> str:
    jid = job_id.strip().lower()
    if not jid or ".." in jid or "/" in jid:
        raise ValueError(f"Invalid spreadsheet engine job id: {job_id!r}")
    return f"{spreadsheet_engine_jobs_prefix()}/{jid}"


def spreadsheet_engine_job_key(job_id: str) -> str:
    return f"{spreadsheet_engine_job_prefix(job_id)}/job.json"


def spreadsheet_engine_job_upload_key(job_id: str, filename: str) -> str:
    name = filename.strip().lstrip("/").replace("\\", "/")
    if not name or ".." in name:
        raise ValueError(f"Invalid upload filename: {filename!r}")
    return f"{spreadsheet_engine_job_prefix(job_id)}/upload/{name}"


def spreadsheet_engine_job_parse_key(job_id: str) -> str:
    return f"{spreadsheet_engine_job_prefix(job_id)}/parse.json"


def spreadsheet_engine_job_profile_key(job_id: str) -> str:
    return f"{spreadsheet_engine_job_prefix(job_id)}/profile.json"


def spreadsheet_engine_job_report_key(job_id: str) -> str:
    return f"{spreadsheet_engine_job_prefix(job_id)}/report.json"


def spreadsheet_engine_job_table_key(job_id: str, table_id: str) -> str:
    tid = table_id.strip().lower()
    if not tid or ".." in tid or "/" in tid:
        raise ValueError(f"Invalid table id: {table_id!r}")
    return f"{spreadsheet_engine_job_prefix(job_id)}/tables/{tid}.json"


def spreadsheet_engine_catalog_prefix() -> str:
    return f"{spreadsheet_engine_prefix()}/catalog"


def spreadsheet_engine_catalog_entry_key(catalog_id: str) -> str:
    cid = catalog_id.strip().lower()
    if not cid or ".." in cid or "/" in cid:
        raise ValueError(f"Invalid spreadsheet engine catalog id: {catalog_id!r}")
    return f"{spreadsheet_engine_catalog_prefix()}/{cid}.json"


def spreadsheet_engine_knowledge_prefix() -> str:
    return f"{spreadsheet_engine_prefix()}/knowledge"


def spreadsheet_engine_knowledge_entry_key(knowledge_id: str) -> str:
    kid = knowledge_id.strip().lower()
    if not kid or ".." in kid or "/" in kid:
        raise ValueError(f"Invalid spreadsheet engine knowledge id: {knowledge_id!r}")
    return f"{spreadsheet_engine_knowledge_prefix()}/{kid}.json"


def prefix_path(data_dir: Path, prefix: str, *parts: str) -> Path:
    segments = [segment for segment in prefix.strip("/").split("/") if segment]
    return data_dir.joinpath(*segments, *parts)


# ── Spreadsheet Lab (parallel, simplified rebuild — see docs/spreadsheet-lab.md) ──
#
# Deliberately isolated from every ``spreadsheet_engine_*``/``SPREADSHEET_REFERENCE_SOURCE``
# key above: same bucket may hold real ``spreadsheet_engine`` job data (e.g. for the
# ``poc`` company today), so the lab uses its own prefix and its own silver source
# name to guarantee it can never read or overwrite the production engine's data.

SPREADSHEET_LAB_REFERENCE_SOURCE = "reference_lab"


def spreadsheet_lab_prefix() -> str:
    """Governance artifacts for the Spreadsheet Lab sandbox engine."""
    return f"{governance_prefix()}/spreadsheet_lab"


def spreadsheet_lab_jobs_prefix() -> str:
    return f"{spreadsheet_lab_prefix()}/jobs"


def spreadsheet_lab_job_prefix(job_id: str) -> str:
    jid = job_id.strip().lower()
    if not jid or ".." in jid or "/" in jid:
        raise ValueError(f"Invalid spreadsheet lab job id: {job_id!r}")
    return f"{spreadsheet_lab_jobs_prefix()}/{jid}"


def spreadsheet_lab_job_key(job_id: str) -> str:
    return f"{spreadsheet_lab_job_prefix(job_id)}/job.json"


def spreadsheet_lab_job_upload_key(job_id: str, filename: str) -> str:
    name = filename.strip().lstrip("/").replace("\\", "/")
    if not name or ".." in name:
        raise ValueError(f"Invalid upload filename: {filename!r}")
    return f"{spreadsheet_lab_job_prefix(job_id)}/upload/{name}"


def spreadsheet_lab_job_parse_key(job_id: str) -> str:
    return f"{spreadsheet_lab_job_prefix(job_id)}/parse.json"


def spreadsheet_lab_job_table_key(job_id: str, table_id: str) -> str:
    tid = table_id.strip().lower()
    if not tid or ".." in tid or "/" in tid:
        raise ValueError(f"Invalid table id: {table_id!r}")
    return f"{spreadsheet_lab_job_prefix(job_id)}/tables/{tid}.json"


def spreadsheet_lab_jobs_list_prefix() -> str:
    return f"{spreadsheet_lab_jobs_prefix()}/"


def spreadsheet_lab_job_tables_prefix(job_id: str) -> str:
    return f"{spreadsheet_lab_job_prefix(job_id)}/tables/"


def spreadsheet_lab_file_recipes_prefix() -> str:
    return f"{spreadsheet_lab_prefix()}/recipes/files"


def spreadsheet_lab_file_recipe_key(shape_hash: str) -> str:
    sid = shape_hash.strip().lower()
    if not sid or ".." in sid or "/" in sid:
        raise ValueError(f"Invalid file recipe shape hash: {shape_hash!r}")
    return f"{spreadsheet_lab_file_recipes_prefix()}/{sid}.json"


def spreadsheet_lab_table_recipes_prefix() -> str:
    return f"{spreadsheet_lab_prefix()}/recipes/tables"


def spreadsheet_lab_table_recipe_key(shape_hash: str) -> str:
    sid = shape_hash.strip().lower()
    if not sid or ".." in sid or "/" in sid:
        raise ValueError(f"Invalid table recipe shape hash: {shape_hash!r}")
    return f"{spreadsheet_lab_table_recipes_prefix()}/{sid}.json"


def spreadsheet_lab_reference_silver_entity_parquet_key(entity: str) -> str:
    """Parquet key for a Spreadsheet Lab reference table — sibling to, and never
    inside, ``spreadsheet_reference_silver_entity_parquet_key``'s ``silver/reference/``."""
    return silver_entity_parquet_key(SPREADSHEET_LAB_REFERENCE_SOURCE, entity)
