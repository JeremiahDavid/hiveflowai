"""Lambda entry point for DNA Engine: API Gateway requests via Mangum, plus
the async self-invoke worker path for the KPI Generator's long-running
Bedrock calls (mirrors ``hiveflow.spreadsheet_lab.web.lambda_handler``'s
``hiveflow_task`` dispatch, and the shell's own former
``kpi_generator_generate`` branch in ``hiveflow.dna.web.lambda_handler`` —
moved here since this is now the Lambda that owns KPI Generator).
"""

from __future__ import annotations

from typing import Any

_mangum = None

KPI_GENERATE_TASK = "kpi_generator_generate"


def _get_mangum() -> Any:
    global _mangum  # noqa: PLW0603 — Lambda container reuse
    if _mangum is None:
        from mangum import Mangum

        from hiveflow.dna_engine.web.app import get_dna_engine_app

        _mangum = Mangum(get_dna_engine_app(), lifespan="off")
    return _mangum


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    payload = event or {}
    if payload.get("hiveflow_task") == KPI_GENERATE_TASK:
        from hiveflow.dna.web.portal.kpi_generator.generation import run_kpi_generation_job
        from hiveflow.dna.web.portal.tenant import resolve_tenant_dna_settings
        from hiveflow.dna_engine.web.tenant import hosting_environment
        from hiveflow.tenant_credentials import tenant_credentials

        environment = hosting_environment()
        settings = resolve_tenant_dna_settings(payload["client_id"], environment)
        with tenant_credentials(settings.company, environment):
            return run_kpi_generation_job(settings, payload)

    return _get_mangum()(event, context)
