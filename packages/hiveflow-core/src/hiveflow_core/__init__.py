"""Shared runtime for HiveFlow agentic components."""

from hiveflow_core.manifest import SourceInfo, hash_file, json_default

__all__ = [
    "AgentRun",
    "BedrockConfig",
    "SourceInfo",
    "build_agent_options",
    "hash_file",
    "json_default",
    "load_env",
    "mcp_tool_name",
    "run_agent",
]

# `agent`/`bedrock` pull in `claude_agent_sdk`, which vendors the full Node.js
# `claude` CLI binary — a hard multi-hundred-MB dependency. Callers that only
# need the plain-Python contracts above (SourceInfo, json_default) shouldn't
# have to bundle that just to import this package, so the SDK-backed names are
# loaded lazily (PEP 562) instead of at module import time.
def __getattr__(name: str):
    if name in ("AgentRun", "run_agent"):
        from hiveflow_core.agent import AgentRun, run_agent

        return {"AgentRun": AgentRun, "run_agent": run_agent}[name]
    if name in ("BedrockConfig", "build_agent_options", "load_env", "mcp_tool_name"):
        from hiveflow_core.bedrock import BedrockConfig, build_agent_options, load_env, mcp_tool_name

        return {
            "BedrockConfig": BedrockConfig,
            "build_agent_options": build_agent_options,
            "load_env": load_env,
            "mcp_tool_name": mcp_tool_name,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
