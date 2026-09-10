"""Runtime shim for the spreadsheet engine's LLM calls.

Two backends, chosen at call time:

* **``sdk``** — the vendored ``hiveflow_core`` wrapper around the Claude Agent SDK.
  Multi-turn, tool-capable, budget-capped. Requires Node.js + the ``claude`` CLI
  on ``PATH`` (only the interpret/propose *container* Lambdas have it). Selected
  when ``HIVEFLOW_AGENT_RUNTIME=sdk`` and the CLI is actually runnable.
* **``converse``** — a single-shot boto3 ``bedrock-runtime`` ``converse`` call.
  No Node, works anywhere (portal Lambda, local dev, zip Lambdas). This mirrors
  the original ``interpret._default_invoke`` and stays the default.

``interpret`` uses :func:`run_tool_agent` when available and otherwise falls back
to its own single-shot path; ``synthesize`` uses :func:`text_invoke`, which
degrades transparently.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any

from botocore.config import Config

DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _budget_from_env() -> float | None:
    raw = os.getenv("MAX_BUDGET_USD", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def agent_runtime() -> str:
    """``"sdk"`` when the Agent SDK backend is usable, else ``"converse"``."""
    if os.getenv("HIVEFLOW_AGENT_RUNTIME", "").strip().lower() != "sdk":
        return "converse"
    # The SDK shells out to the `claude` Node CLI; without it, `query()` fails at
    # runtime. Fall back rather than blow up mid-pipeline.
    if shutil.which("claude") is None:
        return "converse"
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:  # noqa: BLE001
        return "converse"
    return "sdk"


def agent_available() -> bool:
    return agent_runtime() == "sdk"


# ── converse (single-shot, no tools) ────────────────────────────────────────────


def converse_invoke(system: str, user_message: str, *, model: str | None = None) -> str:
    """One ``bedrock-runtime.converse`` turn; returns concatenated text blocks."""
    import boto3

    model_id = (model or os.getenv("HIVEFLOW_BEDROCK_MODEL_ID") or DEFAULT_BEDROCK_MODEL_ID).strip()
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


def text_invoke(
    system: str,
    user_message: str,
    *,
    model: str | None = None,
    max_budget_usd: float | None = None,
) -> str:
    """Drop-in for the old ``_default_invoke``.

    Uses the Agent SDK (no tools, one turn) when it is the active runtime, so the
    call is budget-capped and shares the container's Bedrock wiring; otherwise a
    plain ``converse`` call.
    """
    if agent_runtime() != "sdk":
        return converse_invoke(system, user_message, model=model)

    from hiveflow_core import build_agent_options, run_agent

    async def _run() -> str:
        options = build_agent_options(
            system_prompt=system,
            mcp_servers={},
            allowed_tools=[],
            model=model,
            max_turns=1,
            max_budget_usd=(
                max_budget_usd if max_budget_usd is not None else _budget_from_env()
            ),
        )
        run = await run_agent(user_message, options)
        return run.text

    try:
        return _run_coro(_run())
    except Exception:  # noqa: BLE001 — never let the agent path break a pipeline stage
        return converse_invoke(system, user_message, model=model)


# ── sdk (multi-turn, tools) ────────────────────────────────────────────────────


class AgentRuntimeUnavailable(RuntimeError):
    """Raised by :func:`run_tool_agent` when the ``sdk`` backend is not active."""


@dataclass(slots=True)
class AgentOutcome:
    ok: bool
    text: str
    terminal_reason: str | None
    cost_usd: float | None


def run_tool_agent(
    system_prompt: str,
    user_prompt: str,
    *,
    mcp_servers: dict[str, Any],
    allowed_tools: list[str],
    max_turns: int = 40,
    max_budget_usd: float | None = None,
    model: str | None = None,
) -> AgentOutcome:
    """Run one tool-capable Agent SDK session. Results come from tool side effects.

    Raises :class:`AgentRuntimeUnavailable` unless :func:`agent_available`.
    """
    if agent_runtime() != "sdk":
        raise AgentRuntimeUnavailable("HIVEFLOW_AGENT_RUNTIME != 'sdk' or claude CLI missing")

    from hiveflow_core import build_agent_options, run_agent

    async def _run() -> AgentOutcome:
        options = build_agent_options(
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            model=model,
            max_turns=max_turns,
            max_budget_usd=(
                max_budget_usd if max_budget_usd is not None else _budget_from_env()
            ),
        )
        run = await run_agent(user_prompt, options)
        return AgentOutcome(
            ok=run.ok,
            text=run.text,
            terminal_reason=run.terminal_reason,
            cost_usd=run.total_cost_usd,
        )

    return _run_coro(_run())


# ── asyncio plumbing ──────────────────────────────────────────────────────────


def _run_coro(coro: Any) -> Any:
    """Run ``coro`` to completion whether or not a loop is already running.

    Lambda handlers and CLI calls have no running loop (``asyncio.run`` works);
    the portal's FastAPI/ASGI stack does, so we spawn a dedicated loop thread.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()
