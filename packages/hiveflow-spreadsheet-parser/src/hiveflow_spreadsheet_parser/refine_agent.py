"""Apply phase: turn one free-text `cleanup_instructions` into pandas ops.

The model only proposes a small, fixed vocabulary of operations (below); we apply
them deterministically. It never runs arbitrary code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pandas as pd

from hiveflow_core import build_agent_options, run_agent
from hiveflow_spreadsheet_parser.models import DraftTable
from hiveflow_spreadsheet_parser.prompts import REFINE_SYSTEM, REFINE_USER

_EXPR: dict[str, Callable[[Any], Any]] = {
    "upper": lambda s: s.str.upper(),
    "lower": lambda s: s.str.lower(),
    "strip": lambda s: s.str.strip(),
    "year": lambda s: pd.to_datetime(s, errors="coerce").dt.year,
    "month": lambda s: pd.to_datetime(s, errors="coerce").dt.month,
    "abs": lambda s: pd.to_numeric(s, errors="coerce").abs(),
}


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model reply was not a JSON object")
    return parsed


def apply_operations(df: pd.DataFrame, ops: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[str]]:
    out = df.copy()
    log: list[str] = []
    for op in ops:
        kind = op.get("op")
        if kind == "split_column":
            src = op["source"]
            if src not in out.columns:
                log.append(f"skip split_column: no column {src!r}")
                continue
            parts = (
                out[src]
                .astype("string")
                .str.split(op["separator"], n=len(op["into"]) - 1, regex=bool(op.get("regex")))
            )
            for i, name in enumerate(op["into"]):
                out[name] = parts.str[i].str.strip()
            if op.get("drop_source", True):
                out = out.drop(columns=[src])
            log.append(f"split {src!r} -> {op['into']}")
        elif kind == "regex_replace":
            col = op["column"]
            if col in out.columns:
                out[col] = (
                    out[col]
                    .astype("string")
                    .str.replace(op["pattern"], op.get("replacement", ""), regex=True)
                )
                log.append(f"regex_replace on {col!r}")
        elif kind == "map_values":
            col = op["column"]
            if col in out.columns:
                mapping = op["mapping"]
                default = op.get("default")

                def _remap(v: object, _m: dict[str, Any] = mapping, _d: object = default) -> object:
                    return _m.get(str(v), v if _d is None else _d)

                out[col] = out[col].map(_remap)
                log.append(f"map_values on {col!r} ({len(mapping)} entries)")
        elif kind == "derive_column":
            src = op["from"]
            fn = _EXPR.get(op.get("expression", ""))
            if src in out.columns and fn is not None:
                out[op["name"]] = fn(out[src].astype("string"))
                log.append(f"derived {op['name']!r} = {op['expression']}({src})")
        elif kind == "drop_rows_where":
            col = op["column"]
            if col in out.columns:
                n0 = len(out)
                out = out[out[col].astype("string") != str(op["equals"])].reset_index(drop=True)
                log.append(f"dropped {n0 - len(out)} row(s) where {col} == {op['equals']!r}")
        else:
            log.append(f"ignored unknown op {kind!r}")
    return out, log


async def refine_table(
    table: DraftTable,
    frame: pd.DataFrame,
    *,
    model: str | None = None,
    max_budget_usd: float | None = None,
) -> tuple[pd.DataFrame, list[str], float | None]:
    """Return ``(frame, transforms, cost_usd)``; on any failure the frame is unchanged.

    ``cost_usd`` is the Bedrock spend for this one-shot pass (``None`` if the agent never
    ran, e.g. no ``cleanup_instructions``, or the SDK didn't report a cost).

    ``max_budget_usd`` caps Bedrock spend for this one-shot pass; falls back to
    ``MAX_BUDGET_USD`` from the environment when unset.
    """
    instruction = table.cleanup_instructions.strip()
    if not instruction:
        return frame, [], None
    sample = frame.head(6).to_dict(orient="records")
    prompt = REFINE_USER.format(
        name=table.name,
        columns=list(frame.columns),
        sample=json.dumps(sample, default=str),
        instruction=instruction,
    )
    options = build_agent_options(
        system_prompt=REFINE_SYSTEM,
        mcp_servers={},
        allowed_tools=[],
        model=model,
        max_turns=1,
        max_budget_usd=max_budget_usd,
    )
    run = await run_agent(prompt, options)
    try:
        parsed = _extract_json(run.text)
    except (ValueError, json.JSONDecodeError) as exc:
        return frame, [f"refine skipped: {exc}"], run.total_cost_usd
    if parsed.get("error"):
        return frame, [f"refine declined: {parsed['error']}"], run.total_cost_usd
    ops = parsed.get("operations") or []
    if not ops:
        return frame, ["refine: no operations returned"], run.total_cost_usd
    new_frame, log = apply_operations(frame, ops)
    return new_frame, [f"cleanup_instructions: {instruction}", *log], run.total_cost_usd
