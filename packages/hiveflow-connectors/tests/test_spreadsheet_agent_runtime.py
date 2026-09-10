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
    monkeypatch.setattr(interpret, "agent_available", lambda: True)

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


def test_interpret_tables_agent_failure_falls_back_to_single_shot(monkeypatch):
    """invoke=None + agent path raising -> the single-shot converse call is used."""
    monkeypatch.setattr(interpret, "agent_available", lambda: True)

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
