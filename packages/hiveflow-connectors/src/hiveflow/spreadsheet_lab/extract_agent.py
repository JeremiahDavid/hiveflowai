"""Phase-1 agent call: validate/name a candidate table region, and on rejection
re-examine it with operator feedback.

Table *detection* stays deterministic (``hiveflow.spreadsheet.parser.parse_workbook``
already finds candidate regions well — Excel ListObjects, PivotTables, and a
heuristic contiguous-region scan). This module's job is smaller: confirm one
candidate region, propose its business meaning, and — when an operator rejects
it with feedback — re-inspect that one region with a ``read_range`` tool and
correct its boundaries/headers. It never re-scans the whole workbook from
scratch, and it never persists a raw conversation transcript — a retry is a
fresh call carrying the prior proposal + feedback text, the same pattern
already proven in ``hiveflow.spreadsheet.synthesize``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hiveflow.spreadsheet._agent_runtime import BedrockTool, run_bedrock_tool_agent

_SYSTEM_PROMPT = """You are a table-parsing specialist reviewing ONE candidate table
region that a deterministic detector already found in a spreadsheet. Confirm or
correct it, then propose its business meaning.

Return your proposal ONLY via the `propose_table` tool (not as text), then call
`finish`.

If `operator_feedback` is present in the input, the detector's original region was
wrong in some way the operator describes. First use `read_range`/`get_sheet_map`
to re-inspect the sheet around the given region and figure out the correct
boundaries, then propose corrected `header_row`/`data_start_row`/`data_end_row`/
`min_col`/`max_col`/`headers` via `corrected_region` on your `propose_table` call
(omit `corrected_region`, or set it to null, when the original region was already
correct)."""


def _heuristic_extraction(parse_table: dict[str, Any]) -> dict[str, Any]:
    sheet = str(parse_table.get("sheet") or "")
    headers = [str(h) for h in (parse_table.get("headers") or []) if str(h).strip()]
    entity_name = re.sub(r"[^a-z0-9]+", "_", sheet.strip().lower()).strip("_") or str(
        parse_table.get("table_id") or "table"
    )
    schema = [
        {"name": header, "type": "unknown", "description": f"Column {header}", "is_key": False}
        for header in headers
    ]
    return {
        "entity_name": entity_name,
        "purpose": f"Data extracted from sheet {sheet or 'unknown'}",
        "grain": "one row per record",
        "schema": schema,
        "corrected_region": None,
        "notes": ["Heuristic fallback — agent unavailable or workbook not accessible."],
    }


def propose_table_extraction(
    *,
    workbook_path: str | None,
    parse_table: dict[str, Any],
    feedback: str = "",
    prior_proposal: dict[str, Any] | None = None,
    invoke: Any = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Validate/correct one candidate table region and propose its business meaning.

    ``invoke=False`` skips the agent entirely (tests get the deterministic
    heuristic path); otherwise, when ``workbook_path`` is available, this runs
    one ``run_bedrock_tool_agent`` call with tools bound to the real workbook.
    Falls back to the heuristic proposal on any failure, so this never raises —
    a table always gets *something* to review even if Bedrock is unreachable.
    """
    if invoke is False or not workbook_path:
        return _heuristic_extraction(parse_table)

    try:
        return _agent_propose(
            workbook_path,
            parse_table,
            feedback=feedback,
            prior_proposal=prior_proposal,
            model=model,
        )
    except Exception:  # noqa: BLE001 — never block review on a transient AI failure
        return _heuristic_extraction(parse_table)


def _agent_propose(
    workbook_path: str,
    parse_table: dict[str, Any],
    *,
    feedback: str,
    prior_proposal: dict[str, Any] | None,
    model: str | None,
) -> dict[str, Any]:
    from hiveflow_spreadsheet_parser.readers import read_workbook
    from hiveflow_spreadsheet_parser.tools import (
        ParseSession,
        get_sheet_map_data,
        list_sheets_data,
        read_range_data,
    )

    session = ParseSession(workbook=read_workbook(workbook_path))
    recorded: dict[str, Any] = {}

    def _propose_table(args: dict[str, Any]) -> Any:
        recorded["proposal"] = {
            "entity_name": args.get("entity_name"),
            "purpose": args.get("purpose", ""),
            "grain": args.get("grain", ""),
            "schema": args.get("schema") or [],
            "corrected_region": args.get("corrected_region") or None,
            "notes": args.get("notes") or [],
        }
        return {"recorded": True}

    def _finish(_args: dict[str, Any]) -> Any:
        return {"recorded": bool(recorded)}

    tools = [
        BedrockTool(
            "list_sheets",
            "List every sheet with its size and the detector's candidate regions.",
            {"type": "object", "properties": {}},
            lambda _args: list_sheets_data(session),
        ),
        BedrockTool(
            "get_sheet_map",
            "ASCII layout map, merged ranges, and detector candidates for one sheet.",
            {
                "type": "object",
                "properties": {"sheet": {"type": "string"}},
                "required": ["sheet"],
            },
            lambda args: get_sheet_map_data(session, args["sheet"]),
        ),
        BedrockTool(
            "read_range",
            "Raw cell values for an A1 range (truncated). Use to verify extent and headers.",
            {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string"},
                    "a1_range": {"type": "string"},
                    "max_rows": {"type": "integer"},
                },
                "required": ["sheet", "a1_range"],
            },
            lambda args: read_range_data(
                session, args["sheet"], args["a1_range"], int(args.get("max_rows") or 40)
            ),
        ),
        BedrockTool(
            "propose_table",
            "Record the confirmed/corrected region and its business meaning.",
            {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "grain": {"type": "string"},
                    "schema": {"type": "array", "items": {"type": "object"}},
                    "corrected_region": {
                        "type": ["object", "null"],
                        "properties": {
                            "header_row": {"type": "integer"},
                            "data_start_row": {"type": "integer"},
                            "data_end_row": {"type": "integer"},
                            "min_col": {"type": "integer"},
                            "max_col": {"type": "integer"},
                            "headers": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["entity_name", "grain", "schema"],
            },
            _propose_table,
        ),
        BedrockTool("finish", "Call once propose_table has been recorded.", {}, _finish),
    ]

    payload: dict[str, Any] = {
        "sheet": parse_table.get("sheet"),
        "candidate_region": {
            "header_row": parse_table.get("header_row"),
            "data_start_row": parse_table.get("data_start_row"),
            "data_end_row": parse_table.get("data_end_row"),
            "min_col": parse_table.get("min_col"),
            "max_col": parse_table.get("max_col"),
            "headers": parse_table.get("headers"),
        },
        "sample_rows": parse_table.get("sample_rows"),
    }
    if feedback.strip():
        payload["operator_feedback"] = feedback.strip()
    if prior_proposal:
        payload["prior_proposal"] = prior_proposal

    run_bedrock_tool_agent(
        _SYSTEM_PROMPT,
        json.dumps(payload, default=str),
        tools=tools,
        max_turns=12,
        model=model,
        stop_tool_names=frozenset({"finish"}),
    )
    if not recorded.get("proposal"):
        raise ValueError("Agent did not record a proposal")
    return recorded["proposal"]
