from __future__ import annotations

from typing import Any

from hiveflow.dna.web.asgi import get_asgi_app

_mangum = None


def _get_mangum():
    global _mangum  # noqa: PLW0603 — Lambda container reuse
    if _mangum is None:
        from mangum import Mangum

        # The FastAPI shell owns lifespan; text_mime_types keeps SVG/JS/CSS/JSON
        # out of base64 while images and the .xlsx download stay binary (matches
        # the old BINARY_STATIC_CONTENT_TYPES split under aws-wsgi).
        _mangum = Mangum(
            get_asgi_app(),
            lifespan="off",
            text_mime_types=[
                "application/json",
                "application/javascript",
                "application/xml",
                "image/svg+xml",
            ],
        )
    return _mangum


def _cfn_reporting_init(event: dict[str, Any]) -> dict[str, Any]:
    """CloudFormation Provider onEvent — seed reporting config or no-op on Delete."""
    from hiveflow.dna.init_client import ensure_reporting_config
    from hiveflow.dna.runtime import resolve_dna_settings

    request_type = str(event.get("RequestType", ""))
    props = event.get("ResourceProperties") or {}
    if not isinstance(props, dict):
        props = {}
    company = str(props.get("company") or "").strip()
    pack_id = str(props.get("pack_id") or "").strip()
    physical_id = str(
        event.get("PhysicalResourceId")
        or f"reporting-config-init-{pack_id or company or 'default'}"
    )

    if request_type == "Delete":
        return {
            "PhysicalResourceId": physical_id,
            "Data": {"status": "delete_noop", "pack_id": pack_id},
        }

    settings = resolve_dna_settings(
        event={
            "action": "init-reporting",
            "company": company,
            "pack_id": pack_id,
        }
    )
    result = ensure_reporting_config(settings)
    status = str(result.get("status", ""))
    if status not in {"initialized", "skipped"}:
        raise RuntimeError(f"Reporting config init failed: {result}")
    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "status": status,
            "pack_id": str(result.get("pack_id", pack_id)),
            "reporting_config": str(result.get("reporting_config", "")),
            "version": str(result.get("version", "")),
            "reason": str(result.get("reason", "")),
        },
    }


def ui_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Lambda entry for reporting UI (API Gateway) and ReportingStack seed CR."""
    payload = event or {}
    if payload.get("RequestType") in {"Create", "Update", "Delete"}:
        return _cfn_reporting_init(payload)

    # KPI Generator's async self-invoke task moved to DNA Engine's own
    # Lambda handler (hiveflow.dna_engine.web.lambda_handler) along with the
    # rest of KPI Generator — see docs/dna-engine.md.
    return _get_mangum()(event, context)
