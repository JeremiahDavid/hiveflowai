"""Runtime shim for the spreadsheet engine's LLM calls.

Three call shapes, none of which need Node or the ``claude`` CLI anymore:

* **``converse_invoke`` / ``text_invoke``** — a single-shot, no-tools boto3
  ``bedrock-runtime.converse`` call. :func:`text_invoke` (used by ``synthesize``
  for the propose stage's oracle-clean and transform-synthesis calls) always
  uses this — confirmed via Bedrock's own model-invocation logs that routing a
  single-shot, no-tools call through the old Agent-SDK ``sdk`` backend still
  paid for a full Claude Code CLI session (model-availability probes + an
  unused session-title call) before an internal failure fell back to this
  exact ``converse`` call anyway, so it goes straight here now.
* **``run_bedrock_tool_agent``** — a multi-turn, tool-capable loop driven
  directly by ``bedrock-runtime.converse``'s native ``toolConfig``: each tool
  call the model requests runs in-process immediately and the result feeds
  back into the next turn. ``interpret``'s ``_agent_interpret`` uses this to
  let the model inspect the real workbook (list sheets, read ranges) before
  proposing a schema. This replaced the old Agent-SDK (``sdk``/``claude`` CLI)
  path for the same reason as above: that path was silently failing on every
  real invocation observed (via the same probe-then-title-then-silent-hang
  pattern, just taking ~3 minutes to give up instead of failing instantly),
  so this is a straight port of the same tools onto Bedrock's own tool-use
  protocol — same capability, no CLI subprocess to hang.
* **``run_tool_agent``** — the original vendored ``hiveflow_core`` wrapper
  around the Claude Agent SDK (``sdk`` backend). Still here for any caller
  that genuinely needs the CLI's own tool ecosystem (e.g.
  ``hiveflow_spreadsheet_parser.agent``'s autonomous parse flow), but nothing
  in the deployed spreadsheet-engine pipeline calls it anymore. Requires
  Node.js + the ``claude`` CLI on ``PATH``, selected only when
  ``HIVEFLOW_AGENT_RUNTIME=sdk``.
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
    """Drop-in for the old ``_default_invoke`` — always a plain ``converse`` call.

    A single-shot, no-tools completion has no use for the Agent SDK's
    multi-turn/tool machinery, and routing it through the SDK still pays for a
    full Claude Code CLI session (see module docstring) before falling back to
    this exact call anyway. ``max_budget_usd`` is accepted for interface
    parity with :func:`run_tool_agent` but unused here — ``converse``'s own
    ``maxTokens`` cap already bounds each call's cost tightly.
    """
    del max_budget_usd
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


# ── bedrock-native tools (no Node, no CLI) ──────────────────────────────────────


@dataclass(slots=True)
class BedrockTool:
    """One tool for :func:`run_bedrock_tool_agent` — a name/schema plus the plain
    Python function that runs when the model calls it. ``handler`` takes the
    tool's arguments and returns a JSON-serializable result, or raises."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Any  # Callable[[dict[str, Any]], Any]


def _tool_result_block(tool_use_id: str, payload: Any) -> dict[str, Any]:
    content = [{"text": payload}] if isinstance(payload, str) else [{"json": payload}]
    return {"toolResult": {"toolUseId": tool_use_id, "content": content, "status": "success"}}


def _tool_error_block(tool_use_id: str, message: str) -> dict[str, Any]:
    return {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"text": message}],
            "status": "error",
        }
    }


def run_bedrock_tool_agent(
    system_prompt: str,
    user_prompt: str,
    *,
    tools: list[BedrockTool],
    max_turns: int = 20,
    model: str | None = None,
    stop_tool_names: frozenset[str] = frozenset(),
) -> AgentOutcome:
    """Tool-capable loop driven straight by ``bedrock-runtime.converse`` — no
    Node, no ``claude`` CLI, no Agent SDK session.

    Each tool's ``handler`` runs in-process the moment the model requests it;
    the result is fed straight back as the next turn's ``toolResult``. Stops
    when the model ends its turn without requesting a tool, when it calls a
    tool named in ``stop_tool_names`` (e.g. ``"finish"``), or after
    ``max_turns`` — whichever comes first. There's no CLI subprocess to hang or
    time out, so ``max_turns`` (not a dollar budget) is the only cost guard.
    """
    import boto3

    model_id = (model or os.getenv("HIVEFLOW_BEDROCK_MODEL_ID") or DEFAULT_BEDROCK_MODEL_ID).strip()
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=30, read_timeout=120, retries={"max_attempts": 2}),
    )
    tool_config = {
        "tools": [
            {
                "toolSpec": {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": {"json": t.input_schema or {"type": "object", "properties": {}}},
                }
            }
            for t in tools
        ]
    }
    by_name = {t.name: t for t in tools}
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": user_prompt}]}]

    for _turn in range(max_turns):
        response = client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
        )
        output_message = response.get("output", {}).get("message", {})
        messages.append(output_message)
        content_blocks = output_message.get("content") or []
        tool_uses = [b["toolUse"] for b in content_blocks if isinstance(b, dict) and "toolUse" in b]

        if not tool_uses:
            text = "\n".join(
                b.get("text", "") for b in content_blocks if isinstance(b, dict)
            ).strip()
            return AgentOutcome(ok=True, text=text, terminal_reason=response.get("stopReason"), cost_usd=None)

        result_blocks: list[dict[str, Any]] = []
        stop_now = False
        for use in tool_uses:
            name = str(use.get("name") or "")
            tool_use_id = str(use.get("toolUseId") or "")
            tool = by_name.get(name)
            if tool is None:
                result_blocks.append(_tool_error_block(tool_use_id, f"Unknown tool {name!r}"))
                continue
            try:
                result_blocks.append(_tool_result_block(tool_use_id, tool.handler(use.get("input") or {})))
            except Exception as exc:  # noqa: BLE001 — surfaced to the model, not raised
                result_blocks.append(_tool_error_block(tool_use_id, str(exc)))
            if name in stop_tool_names:
                stop_now = True

        if stop_now:
            return AgentOutcome(ok=True, text="", terminal_reason="stop_tool", cost_usd=None)
        messages.append({"role": "user", "content": result_blocks})

    return AgentOutcome(ok=False, text="", terminal_reason="max_turns", cost_usd=None)


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
