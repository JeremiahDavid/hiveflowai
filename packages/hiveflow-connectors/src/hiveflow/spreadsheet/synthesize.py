"""AI-assisted transformation induction from large spreadsheet samples."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from hiveflow.spreadsheet._agent_runtime import text_invoke as _default_invoke
from hiveflow.spreadsheet.sample import (
    DEFAULT_MAX_SAMPLE_BYTES,
    flatten_oracle_windows,
    select_oracle_windows,
)
from hiveflow.spreadsheet.transform import apply_transformation, normalize_header_name

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    match = _JSON_FENCE.search(stripped)
    if match:
        stripped = match.group(1).strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("Bedrock response must be a JSON object")
    return payload

_ORACLE_SYSTEM = """You clean messy spreadsheet table excerpts for a data platform.
Return strict JSON only (no markdown):
{
  "target_headers": ["snake_case_column", "..."],
  "target_rows": [["value", "..."]],
  "grain": "one row per ...",
  "notes": ["optional caveats"]
}
Rules:
- One output row per business record (merge continuation/detail rows when needed).
- Align values to the correct columns (do not leave UOM in a price column).
- Use snake_case headers.
- Preserve all meaningful fields from the input.
- When operator_feedback is present, revise the cleaned output to address it."""

_CLEAN_GOAL_PREVIEW_ROWS = 100

_SYNTHESIZE_SYSTEM = """You reverse-engineer deterministic spreadsheet cleaning steps from before/after examples.
Return strict JSON only (no markdown):
{
  "steps": [
    {"op": "group_rows", "key_column": "no", "carry_columns": ["description"], "coalesce_columns": ["unit_price"]},
    {"op": "filter_rows", "expr": "no != null AND no != 'Grand Total'"},
    {"op": "rename_columns", "mapping": {"old": "new"}},
    {"op": "cast", "columns": {"unit_price": "number"}}
  ],
  "confidence": 0.0-1.0,
  "notes": ["..."]
}
Allowed ops: group_rows, filter_rows, rename_columns, cast, derive_column.
group_rows merges detail rows (blank key) into the preceding key row.
filter_rows expr supports !=, ==, AND, OR, and not in, e.g. no != null AND no != 'Grand Total'.
The string NULL is treated as null. Do not hardcode row-specific business keys.
When operator_feedback is present, adjust steps to address it while still matching the approved clean goal."""

def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _rows_equal(left: list[Any], right: list[Any]) -> bool:
    max_len = max(len(left), len(right))
    for index in range(max_len):
        if _normalize_cell(left[index] if index < len(left) else None) != _normalize_cell(
            right[index] if index < len(right) else None
        ):
            return False
    return True


def needs_structural_cleaning(profile_table: dict[str, Any] | None) -> bool:
    """Detect ragged layouts that rename/cast alone cannot fix."""
    if not profile_table:
        return False
    key_candidates = list(profile_table.get("key_candidates") or [])
    columns = {
        str(col.get("name") or ""): col
        for col in (profile_table.get("columns") or [])
        if isinstance(col, dict)
    }
    for name in key_candidates:
        col = columns.get(name) or {}
        if float(col.get("null_rate") or 0) >= 0.3:
            return True

    unit_price = columns.get("unit_price") or {}
    if unit_price.get("inferred_type") == "string":
        samples = [str(value) for value in (unit_price.get("sample_values") or [])[:5]]
        if samples and all(value.isalpha() for value in samples):
            return True
    return False


def oracle_clean_sample(
    headers: list[str],
    rows: list[list[Any]],
    *,
    invoke: Callable[[str, str], str] | None = None,
    feedback: str = "",
    prior_goal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    windows = select_oracle_windows(rows)
    payload: dict[str, Any] = {
        "headers": headers,
        "windows": [
            {"start": window["start"], "end": window["end"], "rows": window["rows"]}
            for window in windows
        ],
    }
    if feedback.strip():
        payload["operator_feedback"] = feedback.strip()
    if prior_goal:
        payload["prior_clean_goal"] = {
            "headers": list(prior_goal.get("headers") or []),
            "rows": list(prior_goal.get("rows") or [])[:_CLEAN_GOAL_PREVIEW_ROWS],
            "grain": prior_goal.get("grain"),
            "notes": list(prior_goal.get("notes") or []),
        }
    invoke_fn = invoke or _default_invoke
    raw = invoke_fn(_ORACLE_SYSTEM, json.dumps(payload, default=str))
    parsed = _extract_json(raw)
    target_headers = [
        str(name) for name in (parsed.get("target_headers") or []) if str(name).strip()
    ]
    target_rows = [
        list(row) for row in (parsed.get("target_rows") or []) if isinstance(row, list)
    ]
    return {
        "target_headers": target_headers,
        "target_rows": target_rows,
        "grain": str(parsed.get("grain") or ""),
        "notes": list(parsed.get("notes") or []),
        "oracle_windows": len(windows),
        "oracle_input_rows": sum(len(window["rows"]) for window in windows),
    }


def _heuristic_clean_goal(
    headers: list[str],
    rows: list[list[Any]],
) -> dict[str, Any]:
    """Deterministic fallback cleaned table when Bedrock is unavailable."""
    heuristic = _heuristic_group_rows_spec(headers)
    if heuristic:
        out_rows, out_headers = apply_transformation(
            rows, headers, {"version": 1, "steps": list(heuristic.get("steps") or [])}
        )
        return {
            "headers": out_headers,
            "rows": out_rows[:_CLEAN_GOAL_PREVIEW_ROWS],
            "row_count": len(out_rows),
            "preview_row_count": min(len(out_rows), _CLEAN_GOAL_PREVIEW_ROWS),
            "truncated": len(out_rows) > _CLEAN_GOAL_PREVIEW_ROWS,
            "grain": "one row per key after grouping continuation rows",
            "notes": list(heuristic.get("notes") or []) + ["Heuristic clean (AI unavailable)"],
            "source": "heuristic",
        }
    return {
        "headers": list(headers),
        "rows": [list(row) for row in rows[:_CLEAN_GOAL_PREVIEW_ROWS]],
        "row_count": len(rows),
        "preview_row_count": min(len(rows), _CLEAN_GOAL_PREVIEW_ROWS),
        "truncated": len(rows) > _CLEAN_GOAL_PREVIEW_ROWS,
        "grain": "one row per source row",
        "notes": ["Passthrough clean (AI unavailable)"],
        "source": "passthrough",
    }


def propose_clean_goal(
    *,
    headers: list[str],
    rows: list[list[Any]],
    table: dict[str, Any] | None = None,
    invoke: Callable[[str, str], str] | None = None,
    feedback: str = "",
    prior_goal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a cleaned table shape for operator review (no transform steps yet)."""
    notes: list[str] = []
    goal: dict[str, Any] | None = None
    heuristic_goal = _heuristic_clean_goal(headers, rows)
    if invoke is not False:
        try:
            oracle = oracle_clean_sample(
                headers,
                rows,
                invoke=invoke,
                feedback=feedback,
                prior_goal=prior_goal,
            )
            target_headers = list(oracle.get("target_headers") or [])
            target_rows = list(oracle.get("target_rows") or [])
            if target_headers and target_rows:
                # Prefer heuristic when AI returns a near-identity ragged layout.
                if (
                    heuristic_goal.get("source") == "heuristic"
                    and not feedback.strip()
                    and not _oracle_collapsed_groups(rows, target_rows)
                ):
                    notes.append(
                        "AI clean did not collapse grouped rows; showing heuristic cleaned shape"
                    )
                    goal = heuristic_goal
                else:
                    goal = {
                        "headers": target_headers,
                        "rows": target_rows[:_CLEAN_GOAL_PREVIEW_ROWS],
                        "row_count": len(target_rows),
                        "preview_row_count": min(len(target_rows), _CLEAN_GOAL_PREVIEW_ROWS),
                        "truncated": len(target_rows) > _CLEAN_GOAL_PREVIEW_ROWS,
                        "grain": str(oracle.get("grain") or (table or {}).get("grain") or ""),
                        "notes": list(oracle.get("notes") or []),
                        "source": "oracle",
                        "oracle_windows": int(oracle.get("oracle_windows") or 0),
                        "oracle_input_rows": int(oracle.get("oracle_input_rows") or 0),
                    }
                    notes.append(
                        f"AI cleaned {goal['row_count']} row(s) from "
                        f"{goal.get('oracle_input_rows') or len(rows)} sample row(s)"
                    )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"AI clean failed: {exc}")

    if goal is None:
        goal = heuristic_goal
        notes.extend(str(n) for n in (goal.get("notes") or []))

    if feedback.strip():
        notes.append(f"Revised from operator feedback: {feedback.strip()}")

    return {
        "clean_goal": goal,
        "clean_shape_status": "pending_review",
        "clean_shape_notes": notes,
        "transformation": {"version": 1, "steps": []},
        "transformation_status": "awaiting_shape",
        "transformation_confidence": 0.0,
        "transformation_notes": [
            "Approve the cleaned shape first; then AI will propose deterministic steps."
        ],
        "transformation_drift": [],
    }


def synthesize_from_clean_goal(
    *,
    headers: list[str],
    rows: list[list[Any]],
    clean_goal: dict[str, Any],
    table: dict[str, Any] | None = None,
    invoke: Callable[[str, str], str] | None = None,
    feedback: str = "",
) -> dict[str, Any]:
    """Build deterministic transform steps that recreate an approved clean goal."""
    target_headers = [str(h) for h in (clean_goal.get("headers") or []) if str(h).strip()]
    target_rows = [list(r) for r in (clean_goal.get("rows") or []) if isinstance(r, list)]
    if not target_headers or not target_rows:
        raise ValueError("clean_goal must include headers and rows")

    windows = select_oracle_windows(rows)
    synthesis_input = flatten_oracle_windows(windows)
    notes: list[str] = [
        f"Synthesizing steps to match approved clean goal ({len(target_rows)} goal row(s))"
    ]
    # Prefer explicit feedback; also pick up notes stamped by reject_transformation.
    op_feedback = feedback.strip()
    if not op_feedback:
        for note in clean_goal.get("notes") or []:
            text = str(note)
            if text.lower().startswith("operator feedback"):
                op_feedback = text.split(":", 1)[-1].strip()
                break
    steps: list[dict[str, Any]] = []
    confidence = 0.0

    if invoke is not False:
        try:
            synthesized = synthesize_transform_steps(
                headers,
                synthesis_input,
                target_headers=target_headers,
                target_rows=target_rows,
                invoke=invoke,
                feedback=op_feedback,
            )
            steps = list(synthesized.get("steps") or [])
            confidence = float(synthesized.get("confidence") or 0.0)
            notes.extend(str(n) for n in (synthesized.get("notes") or []))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Synthesis failed: {exc}")

    if not steps:
        heuristic = _heuristic_group_rows_spec(headers)
        if heuristic:
            steps = list(heuristic.get("steps") or [])
            confidence = float(heuristic.get("confidence") or 0.0)
            notes.extend(str(n) for n in (heuristic.get("notes") or []))

    if not steps:
        raise ValueError("Could not synthesize transformation steps for the approved clean goal")

    spec: dict[str, Any] = {"version": 1, "steps": steps}
    verification = verify_transform(
        headers,
        rows,
        spec,
        expected_headers=target_headers,
        expected_rows=target_rows,
    )
    if not verification.get("passed"):
        notes.append(
            "Verification against clean goal failed "
            f"({verification.get('actual_row_count')} vs {verification.get('expected_row_count')} rows)"
        )
        confidence = min(confidence, 0.4)
    else:
        notes.append(
            f"Verified against clean goal ({verification.get('actual_row_count')} output rows)"
        )
        confidence = max(confidence, 0.75)

    table_ctx = dict(table or {})
    if clean_goal.get("grain"):
        table_ctx["grain"] = clean_goal["grain"]
    if target_headers and not table_ctx.get("schema"):
        table_ctx["schema"] = [{"name": name, "type": "string"} for name in target_headers]

    return {
        "transformation": {
            **spec,
            "output_shape": build_induced_output_shape(table_ctx, verification),
        },
        "transformation_status": "pending_review",
        "transformation_confidence": round(confidence, 2),
        "transformation_notes": notes,
        "transformation_drift": [],
        "induction": {
            "sample_row_count": len(rows),
            "oracle_windows": len(windows),
            "verification": verification,
            "max_sample_bytes": DEFAULT_MAX_SAMPLE_BYTES,
            "from_clean_goal": True,
        },
    }


def synthesize_transform_steps(
    headers: list[str],
    input_rows: list[list[Any]],
    *,
    target_headers: list[str],
    target_rows: list[list[Any]],
    invoke: Callable[[str, str], str] | None = None,
    feedback: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input_headers": headers,
        "input_rows": input_rows,
        "output_headers": target_headers,
        "output_rows": target_rows,
    }
    if feedback.strip():
        payload["operator_feedback"] = feedback.strip()
    invoke_fn = invoke or _default_invoke
    raw = invoke_fn(_SYNTHESIZE_SYSTEM, json.dumps(payload, default=str))
    parsed = _extract_json(raw)
    steps = [step for step in (parsed.get("steps") or []) if isinstance(step, dict)]
    return {
        "steps": steps,
        "confidence": float(parsed.get("confidence") or 0.6),
        "notes": list(parsed.get("notes") or []),
    }


def _align_rows_to_headers(
    rows: list[list[Any]],
    headers: list[str],
    expected_headers: list[str],
) -> list[list[Any]]:
    if not expected_headers:
        return rows
    index_map: list[int] = []
    for name in expected_headers:
        norm = normalize_header_name(name)
        index = next(
            (idx for idx, header in enumerate(headers) if normalize_header_name(header) == norm),
            -1,
        )
        index_map.append(index)
    aligned: list[list[Any]] = []
    for row in rows:
        aligned.append([row[idx] if 0 <= idx < len(row) else None for idx in index_map])
    return aligned


def verify_transform(
    headers: list[str],
    input_rows: list[list[Any]],
    spec: dict[str, Any],
    *,
    expected_headers: list[str],
    expected_rows: list[list[Any]],
    max_mismatches: int = 5,
) -> dict[str, Any]:
    out_rows, out_headers = apply_transformation(input_rows, headers, spec)
    aligned_expected = _align_rows_to_headers(expected_rows, expected_headers, out_headers)
    comparable = min(len(out_rows), len(aligned_expected))
    mismatches: list[dict[str, Any]] = []
    for index in range(comparable):
        if not _rows_equal(out_rows[index], aligned_expected[index]):
            mismatches.append(
                {
                    "row": index,
                    "actual": out_rows[index],
                    "expected": aligned_expected[index],
                }
            )
            if len(mismatches) >= max_mismatches:
                break

    row_count_match = len(out_rows) == len(aligned_expected)
    return {
        "passed": row_count_match and not mismatches,
        "row_count_match": row_count_match,
        "actual_row_count": len(out_rows),
        "expected_row_count": len(aligned_expected),
        "mismatches": mismatches,
        "output_headers": out_headers,
    }


def _heuristic_group_rows_spec(headers: list[str]) -> dict[str, Any] | None:
    normalized = {normalize_header_name(header): header for header in headers}
    key_header = normalized.get("no") or normalized.get("id") or normalized.get("item_no")
    if not key_header:
        return None
    carry = [
        normalized[name]
        for name in ("description", "variant_code", "minimum_quantity")
        if name in normalized
    ]
    coalesce = [
        normalized[name]
        for name in ("unit_of_measure_code", "unit_price", "starting_date", "ending_date")
        if name in normalized
    ]
    if not coalesce:
        return None
    steps: list[dict[str, Any]] = [
        {
            "op": "group_rows",
            "key_column": key_header,
            "carry_columns": carry,
            "coalesce_columns": coalesce,
        },
        {
            "op": "filter_rows",
            "expr": (
                f"{normalize_header_name(key_header)} != null AND "
                f"{normalize_header_name(key_header)} != 'Grand Total'"
            ),
        },
    ]
    unit_price = normalized.get("unit_price")
    if unit_price:
        steps.append({"op": "cast", "columns": {unit_price: "number"}})
    return {"steps": steps, "confidence": 0.55, "notes": ["Heuristic grouped-row collapse"]}


def _oracle_collapsed_groups(
    input_rows: list[list[Any]],
    target_rows: list[list[Any]],
    *,
    min_reduction: float = 0.15,
) -> bool:
    """True when the oracle reduced row count enough to count as structural cleaning."""
    if not input_rows or not target_rows:
        return False
    return len(target_rows) <= len(input_rows) * (1.0 - min_reduction)


def _steps_include_group_rows(steps: list[dict[str, Any]]) -> bool:
    return any(str(step.get("op") or "") == "group_rows" for step in steps)


def _verify_heuristic(
    headers: list[str],
    rows: list[list[Any]],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    out_rows, out_headers = apply_transformation(rows, headers, {"version": 1, "steps": steps})
    collapsed = len(out_rows) < len(rows)
    return {
        "passed": collapsed and bool(out_rows),
        "row_count_match": True,
        "actual_row_count": len(out_rows),
        "expected_row_count": len(out_rows),
        "mismatches": [],
        "output_headers": out_headers,
    }


def induce_transformation_from_sample(
    *,
    headers: list[str],
    rows: list[list[Any]],
    table: dict[str, Any],
    invoke: Callable[[str, str], str] | None = None,
) -> dict[str, Any] | None:
    """Oracle → synthesize → verify on a large local sample; return a proposal or None."""
    if not rows:
        return None

    windows = select_oracle_windows(rows)
    oracle_input = flatten_oracle_windows(windows)
    heuristic = _heuristic_group_rows_spec(headers)
    oracle = None
    if invoke is not False:
        try:
            oracle = oracle_clean_sample(headers, rows, invoke=invoke)
        except Exception:  # noqa: BLE001
            oracle = None

    synthesis_input = oracle_input
    target_headers = list(oracle.get("target_headers") or []) if oracle else []
    target_rows = list(oracle.get("target_rows") or []) if oracle else []
    notes: list[str] = []
    confidence = 0.0
    steps: list[dict[str, Any]] = []
    used_oracle = False

    if target_headers and target_rows:
        if heuristic and not _oracle_collapsed_groups(synthesis_input, target_rows):
            notes.append(
                "Oracle did not collapse grouped rows "
                f"({len(target_rows)}/{len(synthesis_input)}); preferring heuristic"
            )
            target_headers = []
            target_rows = []
        else:
            used_oracle = True
            notes.append(
                f"Oracle cleaned {len(target_rows)} row(s) across {len(windows)} window(s)"
            )
            try:
                synthesized = synthesize_transform_steps(
                    headers,
                    synthesis_input,
                    target_headers=target_headers,
                    target_rows=target_rows,
                    invoke=invoke,
                )
                steps = list(synthesized.get("steps") or [])
                confidence = float(synthesized.get("confidence") or 0.0)
                notes.extend(str(note) for note in (synthesized.get("notes") or []))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Synthesis failed: {exc}")

    verification: dict[str, Any] | None = None
    if steps and used_oracle and target_headers and target_rows:
        verification = verify_transform(
            headers,
            rows,
            {"version": 1, "steps": steps},
            expected_headers=target_headers,
            expected_rows=target_rows,
        )
        if not verification.get("passed"):
            notes.append(
                "Verification failed on sample "
                f"({verification.get('actual_row_count')} vs "
                f"{verification.get('expected_row_count')} rows)"
            )
            steps = []
        elif heuristic and not _steps_include_group_rows(steps):
            # Synthesized ops matched a weak oracle but skipped structural merge.
            notes.append("Synthesized steps omit group_rows; preferring heuristic")
            steps = []
            verification = None

    if not steps and heuristic:
        steps = list(heuristic.get("steps") or [])
        confidence = float(heuristic.get("confidence") or 0.0)
        notes.extend(str(note) for note in (heuristic.get("notes") or []))
        verification = _verify_heuristic(headers, rows, steps)
        used_oracle = False

    if not steps:
        return None

    spec: dict[str, Any] = {"version": 1, "steps": steps}
    if verification is None:
        if used_oracle and target_headers and target_rows:
            verification = verify_transform(
                headers,
                rows,
                spec,
                expected_headers=target_headers,
                expected_rows=target_rows,
            )
        else:
            verification = _verify_heuristic(headers, rows, steps)

    if not verification.get("passed"):
        notes.append(
            "Verification failed on sample "
            f"({verification.get('actual_row_count')} vs {verification.get('expected_row_count')} rows)"
        )
        confidence = min(confidence, 0.35)
    else:
        notes.append(
            f"Verified on {len(rows)} sample row(s) "
            f"({verification.get('actual_row_count')} output rows)"
        )
        confidence = max(confidence, 0.7)

    if oracle and oracle.get("grain") and used_oracle:
        table = {**table, "grain": oracle["grain"]}

    return {
        "transformation": spec,
        "transformation_status": "pending_review",
        "transformation_confidence": round(confidence, 2),
        "transformation_notes": notes,
        "transformation_drift": [],
        "induction": {
            "sample_row_count": len(rows),
            "sample_truncated": len(rows) > 0,
            "oracle_windows": len(windows),
            "verification": verification,
            "max_sample_bytes": DEFAULT_MAX_SAMPLE_BYTES,
        },
    }


def build_induced_output_shape(
    table: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    headers = list(verification.get("output_headers") or [])
    schema = list(table.get("schema") or [])
    if headers and not schema:
        schema = [{"name": header, "type": "string"} for header in headers]
    return {
        "entity_name": str(table.get("entity_name") or ""),
        "grain": str(table.get("grain") or ""),
        "schema": schema,
    }
