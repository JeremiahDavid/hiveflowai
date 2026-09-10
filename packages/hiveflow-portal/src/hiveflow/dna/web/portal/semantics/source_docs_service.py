"""Trigger / status / overlay-edit helpers for client gold source-docs."""

from __future__ import annotations

import json
import os
from typing import Any

from hiveflow.dna.settings import DnaSettings
from hiveflow.dna.source_docs.overlays import (
    apply_exclude,
    commit_version,
    list_pending_excludes,
    list_versions,
    restore_version,
    undo_exclude,
)
from hiveflow.dna.source_docs.reference import (
    load_source_docs_gold,
    normalize_reference_source,
    source_supports_gold_build,
)


def _on_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip())


def gold_function_name(*, company: str, environment: str) -> str:
    override = os.getenv("HIVEFLOW_SOURCE_DOCS_GOLD_FUNCTION", "").strip()
    if override:
        return override
    return f"{company.strip().lower()}-{environment.strip().lower()}-bc-source-docs-gold"


def source_docs_gold_status(settings: DnaSettings, *, source: str | None = None) -> dict[str, Any]:
    payload = load_source_docs_gold(settings, source=source)
    connector = str(payload.get("source") or normalize_reference_source(source or settings.source))
    pending = list_pending_excludes(settings, source=connector)
    payload["pending"] = pending
    payload["pending_count"] = len(pending)
    return payload


def enqueue_source_docs_gold_build(
    settings: DnaSettings,
    *,
    company: str,
    environment: str,
    source: str | None = None,
    seed_missing_overlays: bool = True,
    publish_schemas: bool = False,
) -> dict[str, Any]:
    """Enqueue (Lambda) or run locally the gold merge job for one connector source.

    ``publish_schemas`` defaults to False — schemas are published via
    ``scripts/publish_bc_source_docs_schemas.py``, not on every client rebuild.
    """
    connector = normalize_reference_source(source or settings.source) or "dbc"
    if not source_supports_gold_build(connector):
        return {
            "status": "error",
            "reason": "build_unsupported",
            "source": connector,
            "error": f"Gold semantic model build is not available for source {connector!r} yet.",
        }

    payload = {
        "source": connector,
        "seed_missing_overlays": bool(seed_missing_overlays),
        "publish_schemas": bool(publish_schemas),
        "client_bucket": settings.s3_bucket or os.getenv("HIVEFLOW_S3_BUCKET", "").strip() or None,
    }

    if _on_lambda():
        from hiveflow.storage.aws import lambda_client

        function_name = gold_function_name(company=company, environment=environment)
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"
        client = lambda_client(region=region)
        response = client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload, default=str).encode("utf-8"),
        )
        status = int(response.get("StatusCode") or 0)
        if status not in {202, 200}:
            raise RuntimeError(f"Gold build invoke returned status {status}")
        return {
            "status": "enqueued",
            "function_name": function_name,
            "status_code": status,
            "payload": payload,
        }

    try:
        from hiveflow.dna.source_docs.gold import run_source_docs_gold_job
    except ImportError as exc:  # pragma: no cover
        return {
            "status": "error",
            "reason": "connectors_unavailable",
            "error": str(exc),
            "hint": (
                "Install hiveflow-dna or invoke "
                f"{gold_function_name(company=company, environment=environment)} directly."
            ),
        }

    result = run_source_docs_gold_job(
        source=connector,
        client_bucket=payload.get("client_bucket"),
        publish_schemas=bool(publish_schemas),
        seed_missing_overlays=bool(seed_missing_overlays),
        dry_run=False,
    )
    return {"status": "published", "result": result}


def _exclude_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    kind = str(body.get("kind") or "").strip().lower()
    tags = body.get("tags")
    if tags is None and body.get("tag"):
        tags = [str(body.get("tag"))]
    if isinstance(tags, str):
        tags = [tags]
    return {
        "kind": kind,
        "source": str(body.get("source") or "").strip() or None,
        "table": str(body.get("table") or "").strip(),
        "fk": str(body.get("FK") or body.get("fk") or "").strip(),
        "target": str(body.get("target") or "").strip(),
        "silver_entity": str(body.get("silver_entity") or "").strip(),
        "name": str(body.get("name") or "").strip(),
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
    }


def source_docs_exclude(settings: DnaSettings, body: dict[str, Any]) -> dict[str, Any]:
    kwargs = _exclude_kwargs(body)
    if kwargs["kind"] not in {"table", "relationship", "tag"}:
        raise ValueError("kind must be table, relationship, or tag")
    return apply_exclude(settings, **kwargs)


def source_docs_undo_exclude(settings: DnaSettings, body: dict[str, Any]) -> dict[str, Any]:
    kwargs = _exclude_kwargs(body)
    if kwargs["kind"] not in {"table", "relationship", "tag"}:
        raise ValueError("kind must be table, relationship, or tag")
    return undo_exclude(settings, **kwargs)


def source_docs_submit_changes(
    settings: DnaSettings,
    *,
    company: str,
    environment: str,
    source: str | None = None,
    excludes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply client-queued excludes (if any), then enqueue gold merge.

    Version commit is a follow-up after gold is current.
    """
    connector = normalize_reference_source(source or settings.source) or "dbc"
    applied: list[dict[str, Any]] = []
    for raw in excludes or []:
        if not isinstance(raw, dict):
            continue
        body = {"source": connector, **raw}
        result = source_docs_exclude(settings, body)
        applied.append({"request": raw, "changed": result.get("changed")})

    pending = list_pending_excludes(settings, source=connector)
    if not pending:
        return {
            "status": "error",
            "reason": "no_pending",
            "source": connector,
            "error": "No pending overlay excludes to submit.",
            "pending_count": 0,
            "applied": applied,
        }

    build = enqueue_source_docs_gold_build(
        settings,
        company=company,
        environment=environment,
        source=connector,
        seed_missing_overlays=True,
        publish_schemas=False,
    )
    if build.get("status") == "error":
        return {
            **build,
            "pending": pending,
            "pending_count": len(pending),
            "applied": applied,
        }

    # Local sync path: gold is already written — commit version immediately.
    if build.get("status") == "published":
        committed = commit_version(settings, source=connector, note="Submitted")
        return {
            "status": "published",
            "source": connector,
            "pending_count": 0,
            "build": build,
            "version": committed,
            "applied": applied,
        }

    return {
        "status": "enqueued",
        "source": connector,
        "pending": pending,
        "pending_count": len(pending),
        "build": build,
        "commit_required": True,
        "applied": applied,
    }


def source_docs_versions(settings: DnaSettings, *, source: str | None = None) -> dict[str, Any]:
    return list_versions(settings, source=source)


def source_docs_commit_version(
    settings: DnaSettings, *, source: str | None = None, note: str = "Submitted"
) -> dict[str, Any]:
    return commit_version(settings, source=source, note=note)


def source_docs_restore_version(
    settings: DnaSettings, *, version: int, source: str | None = None
) -> dict[str, Any]:
    return restore_version(settings, version=version, source=source)
