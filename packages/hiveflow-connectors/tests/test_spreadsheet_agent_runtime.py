"""Tests for the spreadsheet-engine agent runtime shim and the interpret agent path."""

from __future__ import annotations

import json

import pytest

from hiveflow.spreadsheet import _agent_runtime, interpret


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


_PARSE = {
    "filename": "book.xlsx",
    "tables": [
        {"table_id": "t0", "sheet": "Sheet1", "headers": ["id", "name"],
         "sample_rows": [[1, "a"], [2, "b"]]},
    ],
}
_PROFILE = {
    "tables": [
        {"table_id": "t0", "sheet": "Sheet1", "row_count": 2, "column_count": 2,
         "columns": [
             {"name": "id", "inferred_type": "number", "null_rate": 0.0, "likely_key": True},
             {"name": "name", "inferred_type": "string", "null_rate": 0.0, "likely_key": False},
         ],
         "key_candidates": ["id"]},
    ],
}


def test_interpret_tables_converse_path_shape():
    """invoke=False keeps the deterministic heuristic path and output contract."""
    report = interpret.interpret_tables(_PARSE, _PROFILE, invoke=False)
    assert report["kind"] == "spreadsheet_engine_report"
    assert report["table_count"] == 1
    t = report["tables"][0]
    assert t["table_id"] == "t0"
    assert t["status"] == "pending_review"
    assert {"entity_name", "grain", "schema", "source", "profiling"} <= set(t)


def test_interpret_tables_uses_agent_when_available(monkeypatch):
    """When the agent path is active and populates records, they win over heuristics."""

    def _fake_agent(user_payload, workbook_path, *, model, max_budget_usd):
        assert workbook_path == "/tmp/book.xlsx"
        return (
            {
                "t0": {
                    "table_id": "t0",
                    "entity_name": "widget",
                    "purpose": "agent purpose",
                    "grain": "one row per widget",
                    "confidence": 0.9,
                    "schema": [{"name": "id", "type": "number", "is_key": True}],
                    "relationships": [],
                    "notes": ["from agent"],
                }
            },
            0.0123,
        )

    monkeypatch.setattr(interpret, "_agent_interpret", _fake_agent)
    report = interpret.interpret_tables(
        _PARSE, _PROFILE, workbook_path="/tmp/book.xlsx"
    )
    t = report["tables"][0]
    assert t["entity_name"] == "widget"
    assert t["purpose"] == "agent purpose"
    assert t["notes"] == ["from agent"]
    assert t["status"] == "pending_review"


def test_agent_interpret_drives_real_tools_against_a_real_workbook(monkeypatch, tmp_path):
    """End-to-end: the model's tool_use calls hit the real list_sheets/read_range/
    record_interpretation implementations against an actual .xlsx file, not mocks."""
    from openpyxl import Workbook

    path = tmp_path / "book.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["id", "name"])
    ws.append([1, "a"])
    ws.append([2, "b"])
    wb.save(path)

    _install_fake_bedrock(
        monkeypatch,
        [
            _tool_use_response("list_sheets", tool_use_id="c1"),
            _tool_use_response("read_range", tool_use_id="c2", args={"sheet": "Sheet1", "a1_range": "A1:B3"}),
            _tool_use_response(
                "record_interpretation",
                tool_use_id="c3",
                args={
                    "table_id": "t0",
                    "entity_name": "widget",
                    "grain": "one row per widget",
                    "schema": [{"name": "id", "type": "number"}],
                },
            ),
            _tool_use_response("finish", tool_use_id="c4"),
        ],
    )
    report = interpret.interpret_tables(_PARSE, _PROFILE, workbook_path=str(path))
    t = report["tables"][0]
    assert t["entity_name"] == "widget"
    assert t["grain"] == "one row per widget"


def test_interpret_tables_agent_failure_falls_back_to_single_shot(monkeypatch):
    """invoke=None + agent path raising -> the single-shot converse call is used."""

    def _boom(*_a, **_k):
        raise RuntimeError("no node")

    monkeypatch.setattr(interpret, "_agent_interpret", _boom)

    calls = []

    def _fake_default_invoke(system, user):
        calls.append(system)
        return json.dumps(
            {"tables": [{"table_id": "t0", "entity_name": "from_converse",
                         "grain": "g", "schema": []}]}
        )

    monkeypatch.setattr(interpret, "_default_invoke", _fake_default_invoke)
    report = interpret.interpret_tables(
        _PARSE, _PROFILE, workbook_path="/tmp/book.xlsx"
    )
    assert calls and report["tables"][0]["entity_name"] == "from_converse"
