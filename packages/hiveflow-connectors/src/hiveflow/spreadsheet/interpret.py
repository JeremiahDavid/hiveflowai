"""Bedrock semantic interpretation of profiled spreadsheet tables.

The default path runs a tool-capable Claude Agent SDK session (``_agent_interpret``)
that inspects the actual workbook before proposing an entity name / grain / schema
per table. When the Agent SDK runtime is unavailable (no Node / not a container
Lambda) it falls back to the original single-shot ``converse`` call, and when that
also fails every table gets ``_heuristic_table``. The output dict shape is
identical in all three cases.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from botocore.config import Config

from hiveflow.spreadsheet._agent_runtime import agent_available, run_tool_agent

INTERPRET_KIND = "spreadsheet_engine_report"
DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

_SYSTEM_PROMPT = """You analyze spreadsheet table profiles for a data platform.
Return strict JSON only (no markdown) with this shape:
{
  "tables": [
    {
      "table_id": "t0",
      "entity_name": "snake_case_entity",
      "purpose": "one sentence business purpose",
      "grain": "one row per ...",
      "confidence": 0.0-1.0,
      "schema": [
        {
          "name": "column_name",
          "type": "string|number|date|datetime|boolean|currency|email|unknown",
          "description": "business meaning",
          "nullable": true,
          "is_key": false,
          "is_foreign_key": false,
          "references": null
        }
      ],
      "relationships": [
        {"to_entity": "other_entity", "via_column": "col", "confidence": 0.0-1.0}
      ],
      "notes": ["optional caveats"]
    }
  ]
}
Use profiling stats and samples. Prefer snake_case entity and column names."""


def _default_invoke(system: str, user_message: str) -> str:
    import boto3

    model_id = os.getenv("HIVEFLOW_BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID).strip()
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=30, read_timeout=120, retries={"max_attempts": 2}),
    )
    response = client.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
    )
    content = response.get("output", {}).get("message", {}).get("content") or []
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
    return "\n".join(parts).strip()


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = _JSON_FENCE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("Bedrock response must be a JSON object")
    return payload


def _heuristic_table(
    *,
    table_id: str,
    profile: dict[str, Any],
    parse_table: dict[str, Any] | None,
) -> dict[str, Any]:
    sheet = str(profile.get("sheet") or parse_table.get("sheet") if parse_table else "")
    headers = [str(col.get("name") or "") for col in profile.get("columns") or []]
    entity_name = re.sub(r"[^a-z0-9]+", "_", sheet.strip().lower()).strip("_") or table_id
    schema = []
    for col in profile.get("columns") or []:
        if not isinstance(col, dict):
            continue
        schema.append(
            {
                "name": col.get("name"),
                "type": col.get("inferred_type") or "unknown",
                "description": f"Column {col.get('name')}",
                "nullable": float(col.get("null_rate") or 0) > 0,
                "is_key": bool(col.get("likely_key")),
                "is_foreign_key": False,
                "references": None,
            }
        )
    grain = "one row per record"
    if profile.get("key_candidates"):
        grain = f"one row per {profile['key_candidates'][0]}"
    return {
        "table_id": table_id,
        "entity_name": entity_name,
        "purpose": f"Data extracted from sheet {sheet or 'unknown'}",
        "grain": grain,
        "confidence": 0.35,
        "schema": schema,
        "relationships": [],
        "notes": ["Heuristic fallback — Bedrock unavailable or returned invalid JSON."],
    }


_AGENT_SYSTEM = (
    _SYSTEM_PROMPT
    + """

You are running as an agent with tools over the real workbook:
- list_sheets / get_sheet_map / read_range — inspect layout, headers, and values.
- record_interpretation — call ONCE per table_id below with your proposal
  (entity_name, purpose, grain, confidence, schema, relationships, notes).
- finish — call once after every table_id has a recorded interpretation.

Inspect each region with the tools before recording it. Use the deterministic
profiling stats you are given plus what you read from the sheet. Do not emit the
final JSON as text — the record_interpretation tool calls are the output."""
)


def _agent_interpret(
    user_payload: dict[str, Any],
    workbook_path: str,
    *,
    model: str | None,
    max_budget_usd: float | None,
) -> tuple[dict[str, dict[str, Any]], float | None]:
    """Tool-capable Agent SDK pass. Returns ``({table_id: proposal}, cost_usd)``."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from hiveflow_core import json_default, mcp_tool_name
    from hiveflow_spreadsheet_parser.readers import read_workbook
    from hiveflow_spreadsheet_parser.tools import ParseSession, build_tool_server

    wb = read_workbook(workbook_path)
    session = ParseSession(workbook=wb)
    sheets_server = build_tool_server(session)

    recorded: dict[str, dict[str, Any]] = {}
    valid_ids = {str(t.get("table_id")) for t in user_payload.get("tables") or []}

    def _ok(payload: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(payload, default=json_default)}]}

    @tool(
        "record_interpretation",
        "Record the semantic proposal for one table_id.",
        {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "entity_name": {"type": "string"},
                "purpose": {"type": "string"},
                "grain": {"type": "string"},
                "confidence": {"type": "number"},
                "schema": {"type": "array", "items": {"type": "object"}},
                "relationships": {"type": "array", "items": {"type": "object"}},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["table_id", "entity_name", "grain", "schema"],
            "additionalProperties": True,
        },
    )
    async def record_interpretation(args: dict[str, Any]) -> dict[str, Any]:
        tid = str(args.get("table_id") or "")
        if tid not in valid_ids:
            return {
                "content": [{"type": "text", "text": f"Error: unknown table_id {tid!r}"}],
                "is_error": True,
            }
        recorded[tid] = {
            "table_id": tid,
            "entity_name": args.get("entity_name"),
            "purpose": args.get("purpose", ""),
            "grain": args.get("grain", ""),
            "confidence": args.get("confidence", 0.0),
            "schema": args.get("schema") or [],
            "relationships": args.get("relationships") or [],
            "notes": args.get("notes") or [],
        }
        return _ok({"recorded": tid, "remaining": sorted(valid_ids - set(recorded))})

    @tool("finish", "Call once every table_id has a recorded interpretation.", {})
    async def finish(_args: dict[str, Any]) -> dict[str, Any]:
        return _ok({"recorded": sorted(recorded), "missing": sorted(valid_ids - set(recorded))})

    interpret_server = create_sdk_mcp_server(
        name="interpret", version="0.1.0", tools=[record_interpretation, finish]
    )
    allowed = [
        mcp_tool_name("sheets", "list_sheets"),
        mcp_tool_name("sheets", "get_sheet_map"),
        mcp_tool_name("sheets", "read_range"),
        mcp_tool_name("interpret", "record_interpretation"),
        mcp_tool_name("interpret", "finish"),
    ]
    outcome = run_tool_agent(
        _AGENT_SYSTEM,
        json.dumps(user_payload, default=str),
        mcp_servers={"sheets": sheets_server, "interpret": interpret_server},
        allowed_tools=allowed,
        max_turns=8 + 4 * len(valid_ids),
        max_budget_usd=max_budget_usd,
        model=model,
    )
    return recorded, outcome.cost_usd


def interpret_tables(
    parse_payload: dict[str, Any],
    profile_payload: dict[str, Any],
    *,
    invoke: Callable[[str, str], str] | None = None,
    workbook_path: str | None = None,
    model: str | None = None,
    max_budget_usd: float | None = None,
) -> dict[str, Any]:
    """Produce semantic proposals for each profiled table.

    ``workbook_path`` enables the tool-capable agent pass when the Agent SDK
    runtime is active. ``invoke`` keeps its original tri-state contract
    (``None`` = real Bedrock, callable = custom transport, ``False`` = skip the
    model entirely) so existing callers and tests are unaffected.
    """
    parse_tables = {
        str(t.get("table_id")): t
        for t in (parse_payload.get("tables") or [])
        if isinstance(t, dict) and t.get("table_id")
    }
    profile_tables_list = [
        t for t in (profile_payload.get("tables") or []) if isinstance(t, dict)
    ]
    user_payload = {
        "filename": parse_payload.get("filename"),
        "tables": [
            {
                "table_id": table.get("table_id"),
                "sheet": table.get("sheet"),
                "profiling": table,
                "headers": (parse_tables.get(str(table.get("table_id"))) or {}).get("headers"),
                "sample_rows": (parse_tables.get(str(table.get("table_id"))) or {}).get(
                    "sample_rows"
                ),
            }
            for table in profile_tables_list
        ],
    }
    interpreted: list[dict[str, Any]] = []
    llm_tables: dict[str, dict[str, Any]] = {}

    if invoke is None and workbook_path and agent_available():
        try:
            llm_tables, _cost = _agent_interpret(
                user_payload, workbook_path, model=model, max_budget_usd=max_budget_usd
            )
        except Exception:  # noqa: BLE001 — fall back to single-shot / heuristic
            llm_tables = {}

    if invoke is not False and not llm_tables:
        try:
            invoke_fn = invoke or _default_invoke
            raw = invoke_fn(_SYSTEM_PROMPT, json.dumps(user_payload, default=str))
            parsed = _extract_json(raw)
            for item in parsed.get("tables") or []:
                if isinstance(item, dict) and item.get("table_id"):
                    llm_tables[str(item["table_id"])] = item
        except Exception:  # noqa: BLE001
            llm_tables = {}

    for profile in profile_tables_list:
        table_id = str(profile.get("table_id") or "")
        parse_table = parse_tables.get(table_id)
        proposal = llm_tables.get(table_id)
        if not proposal:
            proposal = _heuristic_table(
                table_id=table_id,
                profile=profile,
                parse_table=parse_table,
            )
        interpreted.append(
            {
                **proposal,
                "status": "pending_review",
                "source": {
                    "sheet": profile.get("sheet") or (parse_table or {}).get("sheet"),
                    "header_row": (parse_table or {}).get("header_row"),
                    "data_start_row": (parse_table or {}).get("data_start_row"),
                    "data_end_row": (parse_table or {}).get("data_end_row"),
                    "row_count": profile.get("row_count"),
                    "column_count": profile.get("column_count"),
                },
                "profiling": profile,
            }
        )

    return {
        "kind": INTERPRET_KIND,
        "filename": parse_payload.get("filename"),
        "table_count": len(interpreted),
        "tables": interpreted,
    }
