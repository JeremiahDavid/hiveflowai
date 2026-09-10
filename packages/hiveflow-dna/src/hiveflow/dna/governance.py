"""Versioned governance artifacts in the tenant data bucket.

Layout::

    governance/{company}_dna_config/workflow.json
    governance/{company}_dna_config/v{semver}/manifest.json
    governance/{company}_dna_config/v{semver}/{company}_dna_config.yaml
    governance/{company}_dna_config/v{semver}/{company}_reporting_config.yaml
    governance/{company}_dna_config/v{semver}/docs/{slug}.md
"""

from __future__ import annotations

import re
from datetime import datetime
from hiveflow.compat import UTC
from typing import Any

from hiveflow.dna.schema import DefinitionPack, load_definition_pack, load_definition_pack_file, starter_pack_path
from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.store import (
    definition_pack_key,
    read_json_artifact,
    read_text_artifact,
    read_yaml_artifact,
    write_json_artifact,
    write_text_artifact,
    write_yaml_artifact,
)
from hiveflow.storage.paths import (
    governance_dna_key,
    governance_dna_legacy_json_key,
    governance_doc_key,
    governance_manifest_key,
    governance_pack_prefix,
    governance_reporting_key,
    governance_reporting_legacy_json_key,
    governance_workflow_key,
    prefix_path,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _doc_slug(filename: str, index: int) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].strip().lower()
    slug = _SLUG_RE.sub("-", stem).strip("-") or f"doc-{index:02d}"
    if not filename.lower().endswith((".md", ".txt", ".markdown")):
        return f"{slug}.md"
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "markdown":
        ext = "md"
    return f"{slug}.{ext}"


def _content_type_for_doc(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".md") or lowered.endswith(".markdown"):
        return "text/markdown; charset=utf-8"
    return "text/plain; charset=utf-8"


def save_governance_version(
    settings: DnaSettings,
    *,
    pack: DefinitionPack,
    reporting: dict[str, Any] | None = None,
    docs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Persist a versioned governance snapshot (DNA YAML + optional reporting YAML + docs)."""
    pack_id = pack.pack_id
    version = pack.version
    company = settings.company or None

    dna_key = governance_dna_key(pack_id, version)
    dna_path = write_yaml_artifact(settings, dna_key, pack.to_dict())

    reporting_path: str | None = None
    reporting_key: str | None = None
    if reporting is not None:
        if not isinstance(reporting, dict):
            raise ValueError("reporting payload must be a mapping")
        reporting_key = governance_reporting_key(pack_id, version, company=company)
        reporting_path = write_yaml_artifact(settings, reporting_key, reporting)

    doc_artifacts: list[dict[str, str]] = []
    for index, doc in enumerate(docs or [], start=1):
        title = str(doc.get("title") or doc.get("filename") or f"Document {index}")
        filename = str(doc.get("filename") or _doc_slug(title, index))
        body = str(doc.get("content") or doc.get("text") or "")
        slug_name = _doc_slug(filename, index)
        key = governance_doc_key(pack_id, version, slug_name)
        path = write_text_artifact(
            settings,
            key,
            body,
            content_type=_content_type_for_doc(slug_name),
        )
        doc_artifacts.append(
            {
                "title": title,
                "filename": slug_name,
                "key": key,
                "path": path,
            }
        )

    artifacts: dict[str, Any] = {
        "dna": {"key": dna_key, "path": dna_path},
        "docs": doc_artifacts,
    }
    if reporting_key and reporting_path:
        artifacts["reporting"] = {"key": reporting_key, "path": reporting_path}

    manifest = {
        "pack_id": pack_id,
        "version": version,
        "company": company or "",
        "status": pack.approval.status or pack.status,
        "saved_at": datetime.now(UTC).isoformat(),
        "artifacts": artifacts,
    }
    manifest_key = governance_manifest_key(pack_id, version)
    manifest_path = write_json_artifact(settings, manifest_key, manifest)

    return {
        "pack_id": pack_id,
        "version": version,
        "dna_path": dna_path,
        "reporting_path": reporting_path,
        "manifest_path": manifest_path,
        "docs": doc_artifacts,
        "manifest": manifest,
    }


def load_governance_dna(
    settings: DnaSettings,
    pack_id: str,
    version: str,
) -> DefinitionPack:
    """Load company DNA config YAML from governance (with legacy fallbacks)."""
    payload = read_yaml_artifact(settings, governance_dna_key(pack_id, version))
    if payload:
        return load_definition_pack(payload)

    # Legacy dna.json under the same pack prefix
    legacy_json = read_json_artifact(settings, governance_dna_legacy_json_key(pack_id, version))
    if legacy_json:
        return load_definition_pack(legacy_json)

    legacy_key = definition_pack_key(pack_id, version).replace(".yaml", ".json")
    legacy = read_json_artifact(settings, legacy_key)
    if legacy:
        return load_definition_pack(legacy)

    legacy_yaml = definition_pack_key(pack_id, version)
    local_yaml = prefix_path(settings.data_dir, legacy_yaml)
    if local_yaml.is_file():
        from hiveflow.dna.schema import load_definition_pack_yaml

        return load_definition_pack_yaml(local_yaml.read_text(encoding="utf-8"))

    starter = starter_pack_path(pack_id)
    if starter.is_file():
        return load_definition_pack_file(starter)
    raise FileNotFoundError(
        f"No governance DNA config found for {pack_id!r} v{version}"
    )


def governance_pack_exists(settings: DnaSettings, pack_id: str) -> bool:
    """True when this company's DNA config pack already has objects under governance/."""
    prefix = governance_pack_prefix(pack_id).rstrip("/") + "/"
    if settings.s3_bucket:
        from hiveflow.storage.aws import s3_client

        response = s3_client().list_objects_v2(
            Bucket=settings.s3_bucket,
            Prefix=prefix,
            MaxKeys=1,
        )
        return bool(response.get("Contents") or response.get("CommonPrefixes"))

    root = prefix_path(settings.data_dir, governance_pack_prefix(pack_id))
    if not root.is_dir():
        return False
    return any(root.rglob("*"))


def load_governance_manifest(
    settings: DnaSettings,
    pack_id: str,
    version: str,
) -> dict[str, Any] | None:
    return read_json_artifact(settings, governance_manifest_key(pack_id, version))


def load_governance_doc(
    settings: DnaSettings,
    pack_id: str,
    version: str,
    filename: str,
) -> str | None:
    return read_text_artifact(settings, governance_doc_key(pack_id, version, filename))


def load_governance_workflow(settings: DnaSettings, pack_id: str) -> dict[str, Any] | None:
    """Prefer governance workflow; fall back to legacy dna/definition_packs path."""
    payload = read_json_artifact(settings, governance_workflow_key(pack_id))
    if payload:
        return payload
    return read_json_artifact(settings, f"dna/definition_packs/{pack_id}/workflow.json")


def load_governance_reporting_payload(
    settings: DnaSettings,
    pack_id: str,
    version: str,
) -> dict[str, Any] | None:
    """Raw reporting YAML/JSON from governance (web layer parses)."""
    company = settings.company or None
    payload = read_yaml_artifact(
        settings,
        governance_reporting_key(pack_id, version, company=company),
    )
    if payload:
        return payload
    return read_json_artifact(settings, governance_reporting_legacy_json_key(pack_id, version))
