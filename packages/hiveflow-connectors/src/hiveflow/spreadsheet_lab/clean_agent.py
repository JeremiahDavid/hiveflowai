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
    return synthesize_from_clean_goal(
        headers=headers,
        rows=rows,
        clean_goal=clean_goal,
        table=table,
        invoke=invoke,
        feedback=feedback,
    )
