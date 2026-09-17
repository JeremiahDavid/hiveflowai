"""Tests for the spreadsheet-engine agent runtime shim."""

from __future__ import annotations

import json

import pytest

from hiveflow.spreadsheet import _agent_runtime


@pytest.fixture(autouse=True)
def _no_sdk(monkeypatch):
    monkeypatch.delenv("HIVEFLOW_AGENT_RUNTIME", raising=False)


def test_agent_runtime_defaults_to_converse(monkeypatch):
    assert _agent_runtime.agent_runtime() == "converse"
    assert _agent_runtime.agent_available() is False


def test_agent_runtime_stays_converse_without_claude_cli(monkeypatch):
    monkeypatch.setenv("HIVEFLOW_AGENT_RUNTIME", "sdk")
    monkeypatch.setattr(_agent_runtime.shutil, "which", lambda _name: None)
    assert _agent_runtime.agent_runtime() == "converse"


def test_run_tool_agent_unavailable_raises(monkeypatch):
    with pytest.raises(_agent_runtime.AgentRuntimeUnavailable):
        _agent_runtime.run_tool_agent(
            "sys", "prompt", mcp_servers={}, allowed_tools=[], max_turns=1
        )


def test_text_invoke_uses_converse_fallback(monkeypatch):
    seen = {}

    def _fake_converse(system, user, *, model=None):
        seen["system"] = system
        seen["user"] = user
        return "converse-result"

    monkeypatch.setattr(_agent_runtime, "converse_invoke", _fake_converse)
    out = _agent_runtime.text_invoke("S", "U")
    assert out == "converse-result"
    assert seen == {"system": "S", "user": "U"}


def test_text_invoke_skips_sdk_even_when_active(monkeypatch):
    """text_invoke is single-shot/no-tools — it must never spin up the Agent
    SDK's Claude Code CLI session, even on the container Lambdas where the sdk
    runtime is otherwise active for run_tool_agent."""
    monkeypatch.setenv("HIVEFLOW_AGENT_RUNTIME", "sdk")
    monkeypatch.setattr(_agent_runtime.shutil, "which", lambda _name: "/usr/bin/claude")
    assert _agent_runtime.agent_runtime() == "sdk"  # sdk really is active here

    def _boom(*_a, **_k):
        raise AssertionError("text_invoke must not touch hiveflow_core")

    import sys

    monkeypatch.setitem(
        sys.modules, "hiveflow_core", type(sys)("hiveflow_core")
    )
    monkeypatch.setattr(sys.modules["hiveflow_core"], "build_agent_options", _boom, raising=False)
    monkeypatch.setattr(sys.modules["hiveflow_core"], "run_agent", _boom, raising=False)

    seen = {}

    def _fake_converse(system, user, *, model=None):
        seen["called"] = (system, user)
        return "ok"

    monkeypatch.setattr(_agent_runtime, "converse_invoke", _fake_converse)
    out = _agent_runtime.text_invoke("S", "U", max_budget_usd=5.0)
    assert out == "ok"
    assert seen["called"] == ("S", "U")


class _FakeBedrockClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _install_fake_bedrock(monkeypatch, responses):
    import sys

    client = _FakeBedrockClient(responses)
    fake_boto3 = type(sys)("boto3")
    fake_boto3.client = lambda *_a, **_k: client
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    return client


def _tool_use_response(name, tool_use_id="t1", args=None):
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": args or {}}}],
            }
        },
    }


def _end_turn_response(text):
    return {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
    }


def test_run_bedrock_tool_agent_executes_tool_then_stops_on_stop_tool(monkeypatch):
    client = _install_fake_bedrock(
        monkeypatch,
        [_tool_use_response("list_sheets"), _tool_use_response("finish", tool_use_id="t2")],
    )
    seen = []
    tools = [
        _agent_runtime.BedrockTool("list_sheets", "d", {"type": "object", "properties": {}}, lambda a: seen.append("listed") or {"ok": True}),
        _agent_runtime.BedrockTool("finish", "d", {}, lambda a: {"done": True}),
    ]
    outcome = _agent_runtime.run_bedrock_tool_agent(
        "sys", "prompt", tools=tools, max_turns=5, stop_tool_names=frozenset({"finish"})
    )
    assert outcome.ok is True
    assert outcome.terminal_reason == "stop_tool"
    assert seen == ["listed"]
    assert len(client.calls) == 2


def test_run_bedrock_tool_agent_returns_text_when_no_tool_use(monkeypatch):
    _install_fake_bedrock(monkeypatch, [_end_turn_response("plain answer")])
    outcome = _agent_runtime.run_bedrock_tool_agent("sys", "prompt", tools=[], max_turns=5)
    assert outcome.ok is True
    assert outcome.text == "plain answer"
    assert outcome.terminal_reason == "end_turn"


def test_run_bedrock_tool_agent_surfaces_handler_errors_and_continues(monkeypatch):
    _install_fake_bedrock(
        monkeypatch,
        [_tool_use_response("boom"), _end_turn_response("recovered")],
    )

    def _boom(_args):
        raise ValueError("nope")

    tools = [_agent_runtime.BedrockTool("boom", "d", {}, _boom)]
    outcome = _agent_runtime.run_bedrock_tool_agent("sys", "prompt", tools=tools, max_turns=5)
    assert outcome.ok is True
    assert outcome.text == "recovered"


def test_run_bedrock_tool_agent_gives_up_after_max_turns(monkeypatch):
    _install_fake_bedrock(monkeypatch, [_tool_use_response("noop")] * 3)
    tools = [_agent_runtime.BedrockTool("noop", "d", {}, lambda a: {})]
    outcome = _agent_runtime.run_bedrock_tool_agent("sys", "prompt", tools=tools, max_turns=3)
    assert outcome.ok is False
    assert outcome.terminal_reason == "max_turns"

