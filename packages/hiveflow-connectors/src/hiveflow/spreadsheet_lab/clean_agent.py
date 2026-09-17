"""Phase-2 agent calls: propose a cleaned shape, then synthesize deterministic
transform steps that reproduce it.

This is almost entirely reuse — no new agent logic. ``hiveflow.spreadsheet.synthesize``
already implements exactly the oracle-clean → operator-approve → step-synthesis
→ verify flow this phase needs, including the fresh-context feedback pattern
(``feedback=...``, ``prior_goal=...``). Only the orchestration around it — which
table, which storage keys, which recipe — is new (see ``clean_review.py``).
"""

from __future__ import annotations

from typing import Any

from hiveflow.spreadsheet.synthesize import propose_clean_goal, synthesize_from_clean_goal
from hiveflow.spreadsheet.transform import build_output_shape


def propose_clean(
    *,
    headers: list[str],
    rows: list[list[Any]],
    table: dict[str, Any] | None = None,
    feedback: str = "",
    prior_goal: dict[str, Any] | None = None,
    invoke: Any = None,
) -> dict[str, Any]:
    return propose_clean_goal(
        headers=headers,
        rows=rows,
        table=table,
        invoke=invoke,
        feedback=feedback,
        prior_goal=prior_goal,
    )


def synthesize_clean(
    *,
    headers: list[str],
    rows: list[list[Any]],
    clean_goal: dict[str, Any],
    table: dict[str, Any] | None = None,
    feedback: str = "",
    invoke: Any = None,
) -> dict[str, Any]:
    if not (clean_goal.get("rows") or []):
        # An approved clean goal with zero rows means the table genuinely has
        # no data this run (e.g. an optional report section left blank, or a
        # boilerplate metadata table with nothing populated) — confirmed by a
        # real BC report table ("Request Page Option") whose heuristic-
        # fallback clean_goal was a well-formed {headers: [...], rows: []}.
        # hiveflow.spreadsheet.synthesize.synthesize_from_clean_goal requires
        # at least one target row (it needs one to verify a candidate
        # transform against), so there is genuinely nothing for it to
        # synthesize FROM here — the correct transform is trivially "keep
        # nothing," not an error.
        return _empty_transformation(clean_goal, table)
    return synthesize_from_clean_goal(
        headers=headers,
        rows=rows,
        clean_goal=clean_goal,
        table=table,
        invoke=invoke,
        feedback=feedback,
    )


def _empty_transformation(
    clean_goal: dict[str, Any], table: dict[str, Any] | None
) -> dict[str, Any]:
    target_headers = [str(h) for h in (clean_goal.get("headers") or []) if str(h).strip()]
    table_ctx = dict(table or {})
    if clean_goal.get("grain"):
        table_ctx["grain"] = clean_goal["grain"]
    if target_headers and not table_ctx.get("schema"):
        table_ctx["schema"] = [{"name": name, "type": "string"} for name in target_headers]
    # A column compared to itself is always "equal" (including two Nones),
    # so `!=` on it is a safe, always-false filter using the same expression
    # grammar apply_transformation already supports — no new step type
    # needed to reliably drop every row regardless of what the full raw
    # range (not just the review sample) actually contains.
    steps = [{"op": "filter_rows", "expr": f"{target_headers[0]} != {target_headers[0]}"}] if target_headers else []
    return {
        "transformation": {
            "version": 1,
            "steps": steps,
            "output_shape": build_output_shape(table_ctx),
        },
        "transformation_status": "pending_review",
        "transformation_confidence": 1.0,
        "transformation_notes": [
            "Clean goal has no data rows — nothing to transform; approving will materialize 0 rows with the approved headers."
        ],
        "transformation_drift": [],
    }
