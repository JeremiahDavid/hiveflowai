"""In-process MCP tools the propose agent uses to inspect and stage tables."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from hiveflow_core import json_default
from hiveflow_spreadsheet_parser.detect import Region, detect_workbook
from hiveflow_spreadsheet_parser.discover import build_draft_table
from hiveflow_spreadsheet_parser.grid import overview
from hiveflow_spreadsheet_parser.models import DraftManifest, DraftTable, SkippedRegion
from hiveflow_spreadsheet_parser.readers.base import Workbook, cell_a1, parse_a1_range

SERVER_NAME = "sheets"
TOOL_NAMES = (
    "list_sheets",
    "get_sheet_map",
    "read_range",
    "profile_region",
    "discard_region",
    "finalize",
)


def _text(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=json_default, indent=2)}]
    }


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {msg}"}], "is_error": True}


@dataclass(slots=True)
class ParseSession:
    workbook: Workbook
    detected: dict[str, list[Region]] = field(default_factory=dict)
    staged: dict[str, DraftTable] = field(default_factory=dict)
    skipped: list[SkippedRegion] = field(default_factory=list)
    summary: str = ""
    finalized: bool = False

    def __post_init__(self) -> None:
        if not self.detected:
            self.detected = detect_workbook(self.workbook.sheets)

    def to_manifest(self, source: Any) -> DraftManifest:
        warnings: list[str] = []
        if self.summary:
            warnings.append(f"agent summary: {self.summary}")
        tables = sorted(self.staged.values(), key=lambda t: (t.sheet, t.a1_range))
        return DraftManifest(
            source=source, tables=tables, skipped=list(self.skipped), warnings=warnings
        )


def list_sheets_data(session: ParseSession) -> dict[str, Any]:
    """Every sheet's size and the detector's candidate regions."""
    wb = session.workbook
    out = []
    for grid in wb.sheets:
        ov = overview(grid)
        out.append(
            {
                "sheet": grid.name,
                "rows": grid.nrows,
                "cols": grid.ncols,
                "density": round(ov.density, 2),
                "merged_ranges": len(grid.merged_ranges),
                "candidates": [
                    {
                        "a1_range": r.a1_range,
                        "kind": r.kind,
                        "confidence": r.confidence,
                        "header_rows": r.header_rows,
                    }
                    for r in session.detected.get(grid.name, [])
                ],
            }
        )
    return {"sheets": out}


def get_sheet_map_data(session: ParseSession, sheet: str) -> dict[str, Any]:
    """ASCII layout map, merged ranges, and detailed detector candidates for one sheet.

    Raises ``KeyError`` when ``sheet`` doesn't exist.
    """
    wb = session.workbook
    grid = wb.sheet(sheet)
    ov = overview(grid)
    merged = [grid.range_a1(*m) for m in grid.merged_ranges]
    cands = [
        {
            "a1_range": r.a1_range,
            "kind": r.kind,
            "confidence": r.confidence,
            "header_rows": r.header_rows,
            "signals": r.signals,
            "features": r.features,
        }
        for r in session.detected.get(sheet, [])
    ]
    return {
        "sheet": sheet,
        "shape_map": ov.shape_map,
        "legend": "' '=empty  '.'/':'/'#'=increasing fill; each char is one cell",
        "empty_row_runs": ov.empty_row_runs,
        "merged_ranges": merged,
        "candidates": cands,
    }


def read_range_data(
    session: ParseSession, sheet: str, a1_range: str, max_rows: int = 40
) -> dict[str, Any]:
    """Raw cell values for an A1 range (truncated).

    Raises ``KeyError``/``ValueError`` for an unknown sheet or malformed range.
    """
    wb = session.workbook
    grid = wb.sheet(sheet)
    r0, c0, r1, c1 = parse_a1_range(a1_range)
    r1 = min(r1, grid.nrows - 1, r0 + max_rows - 1)
    c1 = min(c1, grid.ncols - 1, c0 + 39)
    rows = []
    for r in range(r0, r1 + 1):
        rows.append(
            {
                cell_a1(r, c): _short(grid.cell(r, c).value)
                for c in range(c0, c1 + 1)
                if grid.cell(r, c).occupied
            }
        )
    return {"sheet": sheet, "range": grid.range_a1(r0, c0, r1, c1), "rows_by_cell": rows}


def build_tool_server(session: ParseSession) -> Any:
    @tool("list_sheets", "List every sheet with its size and the detector's candidate regions.", {})
    async def list_sheets(_args: dict[str, Any]) -> dict[str, Any]:
        return _text(list_sheets_data(session))

    @tool(
        "get_sheet_map",
        "ASCII layout map, merged ranges, and detailed detector candidates for one sheet.",
        {
            "type": "object",
            "properties": {"sheet": {"type": "string"}},
            "required": ["sheet"],
            "additionalProperties": False,
        },
    )
    async def get_sheet_map(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(get_sheet_map_data(session, args["sheet"]))
        except KeyError as exc:
            return _err(str(exc))

    @tool(
        "read_range",
        "Raw cell values for an A1 range (truncated). Use to verify extent and headers.",
        {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "a1_range": {"type": "string"},
                "max_rows": {"type": "integer", "default": 40},
            },
            "required": ["sheet", "a1_range"],
            "additionalProperties": False,
        },
    )
    async def read_range(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _text(
                read_range_data(
                    session, args["sheet"], args["a1_range"], int(args.get("max_rows") or 40)
                )
            )
        except (KeyError, ValueError) as exc:
            return _err(str(exc))

    @tool(
        "profile_region",
        "Extract + profile an A1 range as one table and stage it for review.",
        {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "a1_range": {"type": "string"},
                "name": {"type": "string", "description": "short snake_case content name"},
                "header_rows": {"type": "integer", "default": 1},
                "orientation": {"type": "string", "enum": ["rows", "columns"], "default": "rows"},
                "row_group_key": {
                    "type": "string",
                    "description": (
                        "Set when each logical record is wrapped across more than one "
                        "physical row (e.g. an ID alone on one row, its remaining fields "
                        "on the row(s) below). Name the column that is populated only on "
                        "the row where a new record starts; every row after it, up to the "
                        "next populated one, is folded into that record."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": ["table", "matrix", "key_value", "list", "unknown"],
                    "default": "table",
                },
                "notes": {"type": "string", "default": ""},
            },
            "required": ["sheet", "a1_range", "name"],
            "additionalProperties": False,
        },
    )
    async def profile_region(args: dict[str, Any]) -> dict[str, Any]:
        try:
            grid = session.workbook.sheet(args["sheet"])
        except KeyError as exc:
            return _err(str(exc))
        try:
            draft = build_draft_table(
                grid,
                args["a1_range"],
                kind=args.get("kind", "table"),
                confidence=_match_conf(session, args["sheet"], args["a1_range"]),
                header_rows=int(args.get("header_rows") or 1),
                orientation=args.get("orientation", "rows"),
                row_group_key=args.get("row_group_key") or None,
                name=args["name"],
            )
        except Exception as exc:
            return _err(f"could not profile {args['a1_range']}: {exc}")
        draft.agent_notes = args.get("notes", "")
        session.staged[draft.id] = draft
        return _text(
            {
                "staged": draft.id,
                "name": draft.name,
                "profile": draft.profile.model_dump(),
                "columns": [
                    {
                        "name": c.name,
                        "dtype": c.dtype,
                        "semantic": c.semantic,
                        "null_count": c.null_count,
                        "distinct_count": c.distinct_count,
                        "sample_values": c.sample_values,
                    }
                    for c in draft.columns
                ],
                "sample_rows": [
                    dict(zip((c.name for c in draft.columns), row, strict=False))
                    for row in _extract_sample(grid, draft)
                ],
            }
        )

    @tool(
        "discard_region",
        "Record a candidate you are deliberately not staging.",
        {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "a1_range": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["sheet", "a1_range", "reason"],
            "additionalProperties": False,
        },
    )
    async def discard_region(args: dict[str, Any]) -> dict[str, Any]:
        session.skipped.append(
            SkippedRegion(sheet=args["sheet"], a1_range=args["a1_range"], reason=args["reason"])
        )
        session.staged.pop(f"{args['sheet']}!{args['a1_range']}", None)
        return _text({"discarded": f"{args['sheet']}!{args['a1_range']}"})

    @tool(
        "finalize",
        "Call once when every sheet has been handled.",
        {
            "type": "object",
            "properties": {"summary": {"type": "string", "default": ""}},
            "additionalProperties": False,
        },
    )
    async def finalize(args: dict[str, Any]) -> dict[str, Any]:
        session.summary = args.get("summary", "")
        session.finalized = True
        return _text(
            {
                "staged_tables": sorted(session.staged),
                "skipped": [s.model_dump() for s in session.skipped],
            }
        )

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.1.0",
        tools=[
            list_sheets,
            get_sheet_map,
            read_range,
            profile_region,
            discard_region,
            finalize,
        ],
    )


def _short(value: Any, limit: int = 60) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _match_conf(session: ParseSession, sheet: str, a1_range: str) -> float:
    for r in session.detected.get(sheet, []):
        if r.a1_range == a1_range:
            return r.confidence
    return 0.6


def _extract_sample(grid: Any, draft: DraftTable, n: int = 6) -> list[list[Any]]:
    from hiveflow_spreadsheet_parser.extract import ExtractOptions, extract_region

    ex = extract_region(
        grid,
        draft.a1_range,
        ExtractOptions(
            header_rows=draft.header_rows,
            orientation=draft.orientation,
            row_group_key=draft.row_group_key,
        ),
    )
    return ex.rows[:n]
