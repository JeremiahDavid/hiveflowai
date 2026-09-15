"""Lambda entry point for Spreadsheet Lab: API Gateway requests via Mangum,
plus the async self-invoke worker path (see ``hiveflow.spreadsheet_lab.worker``).
"""

from __future__ import annotations

from typing import Any

_mangum = None


def _get_mangum() -> Any:
    global _mangum  # noqa: PLW0603 — Lambda container reuse
    if _mangum is None:
        from mangum import Mangum

        from hiveflow.spreadsheet_lab.web.app import get_spreadsheet_lab_app

        _mangum = Mangum(get_spreadsheet_lab_app(), lifespan="off")
    return _mangum


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    payload = event or {}
    from hiveflow.spreadsheet_lab.worker import TABLE_TASK, run_table_task

    if payload.get("hiveflow_task") == TABLE_TASK:
        return run_table_task(payload)

    return _get_mangum()(event, context)
