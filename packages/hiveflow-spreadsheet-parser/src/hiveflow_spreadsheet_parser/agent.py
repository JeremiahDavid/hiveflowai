"""Propose phase: run the Bedrock agent to discover and stage every table."""

from __future__ import annotations

from dataclasses import dataclass

from hiveflow_core import AgentRun, SourceInfo, build_agent_options, mcp_tool_name, run_agent
from hiveflow_spreadsheet_parser import __version__
from hiveflow_spreadsheet_parser.discover import build_draft
from hiveflow_spreadsheet_parser.models import DraftManifest
from hiveflow_spreadsheet_parser.prompts import PROPOSE_SYSTEM, PROPOSE_USER
from hiveflow_spreadsheet_parser.readers import read_workbook
from hiveflow_spreadsheet_parser.tools import (
    SERVER_NAME,
    TOOL_NAMES,
    ParseSession,
    build_tool_server,
)


@dataclass(slots=True)
class ProposeResult:
    manifest: DraftManifest
    run: AgentRun | None
    used_agent: bool


async def propose(
    path: str,
    *,
    model: str | None = None,
    max_turns: int = 80,
    max_budget_usd: float | None = None,
    use_agent: bool = True,
) -> ProposeResult:
    """Build a draft manifest for ``path``.

    With ``use_agent`` (default) a Bedrock-hosted Claude walks the workbook via the
    ``sheets`` MCP tools; its staged tables become the manifest. If it stages
    nothing, or ``use_agent`` is off, fall back to pure heuristic detection.

    ``max_budget_usd`` caps total Bedrock spend for this run; falls back to
    ``MAX_BUDGET_USD`` from the environment when unset (see
    ``hiveflow_core.build_agent_options``).
    """
    wb = read_workbook(path)
    source = SourceInfo.for_file(path, fmt=wb.fmt, parser_version=__version__)

    if not use_agent:
        return ProposeResult(manifest=build_draft(wb, source), run=None, used_agent=False)

    session = ParseSession(workbook=wb)
    server = build_tool_server(session)
    options = build_agent_options(
        system_prompt=PROPOSE_SYSTEM,
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[mcp_tool_name(SERVER_NAME, name) for name in TOOL_NAMES],
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
    )
    run = await run_agent(PROPOSE_USER.format(sheets=wb.sheet_names), options)

    if session.staged:
        manifest = session.to_manifest(source)
        if not run.ok:
            manifest.warnings.append(f"agent ended early: {run.terminal_reason}")
        return ProposeResult(manifest=manifest, run=run, used_agent=True)

    manifest = build_draft(wb, source)
    manifest.warnings.append(
        "agent staged no tables; fell back to heuristic detection"
        + (f" (terminal_reason={run.terminal_reason})" if run.terminal_reason else "")
    )
    return ProposeResult(manifest=manifest, run=run, used_agent=False)
