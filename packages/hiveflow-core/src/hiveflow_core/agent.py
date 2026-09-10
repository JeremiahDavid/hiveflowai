"""Drive a Claude Agent SDK session to completion and collect its output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    query,
)


@dataclass(slots=True)
class ToolCall:
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class AgentRun:
    """Everything worth keeping from one `query()` session."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    terminal_reason: str | None = None
    result: object | None = None
    num_turns: int = 0
    total_cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.terminal_reason in (None, "success")


async def run_agent(prompt: str, options: ClaudeAgentOptions) -> AgentRun:
    """Run one stateless agent session, returning collected text and tool calls.

    Structured results should be pulled from the state your MCP tool handlers
    mutate, not parsed out of ``AgentRun.text``.
    """
    text: list[str] = []
    thinking: list[str] = []
    calls: list[ToolCall] = []
    terminal_reason: str | None = None
    result: object | None = None
    total_cost_usd: float | None = None
    turns = 0

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            turns += 1
            for block in message.content:
                if isinstance(block, TextBlock):
                    text.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    thinking.append(block.thinking)
                elif isinstance(block, ToolUseBlock):
                    calls.append(ToolCall(name=block.name, input=block.input))
        elif isinstance(message, ResultMessage):
            terminal_reason = getattr(message, "terminal_reason", None)
            result = getattr(message, "result", None)
            total_cost_usd = getattr(message, "total_cost_usd", None)

    return AgentRun(
        text="".join(text).strip(),
        tool_calls=calls,
        thinking="\n".join(thinking).strip(),
        terminal_reason=terminal_reason,
        result=result,
        num_turns=turns,
        total_cost_usd=total_cost_usd,
    )
