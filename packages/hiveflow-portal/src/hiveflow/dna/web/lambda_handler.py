from __future__ import annotations

import os
from typing import Any

from hiveflow.dna.web.asgi import get_asgi_app

_mangum = None


def _worker_dna_settings(payload: dict[str, Any]):
    """Rebuild tenant ``DnaSettings`` for an async worker event.

    Multi-tenant: the shared Lambda has no tenant env vars, so bind from the
    event's ``client_id`` via the registry. Single-tenant (legacy ReportingStack):
    fall back to env-var resolution.
    """
    from hiveflow.dna.runtime import resolve_dna_settings

    client_id = str(payload.get("client_id") or "").strip()
    multitenant = os.getenv("HIVEFLOW_UI_MODE", "").strip().lower() == "reporting_multitenant"
    if client_id and multitenant:
        from hiveflow.dna.web.portal.tenant import resolve_tenant_dna_settings
        from hiveflow.project_config import resolve_selection

        _company, environment = resolve_selection()
        return resolve_tenant_dna_settings(client_id, environment)
    # Legacy single-tenant ReportingStack: env vars carry the tenant.
    return resolve_dna_settings()


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

    task = str(payload.get("hiveflow_task") or "").strip()
    if task == "kpi_generator_generate":
        from hiveflow.dna.web.portal.kpi_generator.generation import run_kpi_generation_job
        from hiveflow.dna.web.portal.tenant_credentials import tenant_credentials
        from hiveflow.project_config import resolve_selection

        settings = _worker_dna_settings(payload)
        _company, environment = resolve_selection()
        # No-op unless the multi-tenant Lambda is configured to assume roles.
        with tenant_credentials(settings.company, environment):
            return run_kpi_generation_job(settings, payload)

    return _get_mangum()(event, context)
