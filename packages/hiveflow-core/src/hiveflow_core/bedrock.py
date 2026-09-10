"""Wire the Claude Agent SDK to Amazon Bedrock from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions
from dotenv import load_dotenv

DEFAULT_MODEL = "us.anthropic.claude-sonnet-5"
DEFAULT_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Env vars forwarded to the SDK subprocess so it targets Bedrock.
_PASSTHROUGH = (
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_MAX_RETRIES",
)

_loaded = False


def load_env(dotenv_path: str | Path | None = None) -> None:
    """Load a ``.env`` once. Safe to call repeatedly."""
    global _loaded
    if _loaded and dotenv_path is None:
        return
    load_dotenv(dotenv_path, override=False)
    _loaded = True


def mcp_tool_name(server: str, tool: str) -> str:
    """SDK-visible name for an in-process MCP tool: ``mcp__<server>__<tool>``."""
    return f"mcp__{server}__{tool}"


def _float_env(key: str) -> float | None:
    raw = os.environ.get(key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass(slots=True)
class BedrockConfig:
    """Resolved Bedrock settings for one agent run."""

    region: str
    model: str
    haiku_model: str
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, model: str | None = None) -> BedrockConfig:
        load_env()
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        resolved_model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        haiku = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or DEFAULT_HAIKU_MODEL

        env: dict[str, str] = {
            "CLAUDE_CODE_USE_BEDROCK": os.environ.get("CLAUDE_CODE_USE_BEDROCK", "1"),
            "AWS_REGION": region,
            "ANTHROPIC_MODEL": resolved_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku,
        }
        for key in _PASSTHROUGH:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        return cls(region=region, model=resolved_model, haiku_model=haiku, env=env)


def build_agent_options(
    *,
    system_prompt: str,
    mcp_servers: dict[str, Any],
    allowed_tools: list[str],
    model: str | None = None,
    max_turns: int | None = 60,
    max_budget_usd: float | None = None,
    cwd: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    """Build headless ``ClaudeAgentOptions`` pointed at Bedrock.

    Headless posture: no filesystem settings are read (``setting_sources=[]``),
    only the MCP servers passed here are available (``strict_mcp_config=True``),
    and any tool not in ``allowed_tools`` is denied without a prompt
    (``permission_mode="dontAsk"``).

    ``max_budget_usd`` is a hard per-run USD ceiling on Bedrock spend (input +
    output + thinking tokens all count against it); the SDK ends the run once
    it's hit. Pass it explicitly, or set ``MAX_BUDGET_USD`` in the environment
    (loaded from ``.env``) to apply a default to every run that doesn't
    override it.

    Deliberately leaves ``ClaudeAgentOptions.model`` unset: that field is
    resolved against the CLI's own direct-API alias table (``sonnet`` /
    ``opus`` / ``claude-sonnet-5`` ...), not raw Bedrock inference-profile
    ARNs — passing one there fails to resolve and the CLI silently falls back
    to its own default (Opus) instead of honoring ``ANTHROPIC_MODEL``. The
    ``ANTHROPIC_MODEL`` env var below is the correct, and only, channel for
    picking the Bedrock model; ``model=``/``--model`` override it by feeding
    back into that same env var via ``BedrockConfig.from_env``.
    """
    cfg = BedrockConfig.from_env(model=model)
    env = dict(cfg.env)
    if extra_env:
        env.update(extra_env)
    budget = max_budget_usd if max_budget_usd is not None else _float_env("MAX_BUDGET_USD")

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=max_turns,
        max_budget_usd=budget,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )
