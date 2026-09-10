"""build_agent_options(): headless posture + the MAX_BUDGET_USD spend-cap default."""

from __future__ import annotations

import pytest

from hiveflow_core import bedrock
from hiveflow_core.bedrock import build_agent_options


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    # load_env() re-reads the repo-root .env on every call (override=False just
    # means "don't clobber a var that's already set", so a deleted var still gets
    # refilled from disk). Mark it already-loaded so these tests see only the env
    # this test file sets, never the developer's real .env content.
    monkeypatch.setattr(bedrock, "_loaded", True)


def _options(**kwargs: object):
    return build_agent_options(
        system_prompt="test",
        mcp_servers={},
        allowed_tools=[],
        **kwargs,
    )


def test_no_budget_cap_by_default(monkeypatch):
    monkeypatch.delenv("MAX_BUDGET_USD", raising=False)
    assert _options().max_budget_usd is None


def test_explicit_arg_wins_over_no_env(monkeypatch):
    monkeypatch.delenv("MAX_BUDGET_USD", raising=False)
    assert _options(max_budget_usd=2.5).max_budget_usd == 2.5


def test_env_default_applies_when_arg_omitted(monkeypatch):
    monkeypatch.setenv("MAX_BUDGET_USD", "1.00")
    assert _options().max_budget_usd == 1.0


def test_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("MAX_BUDGET_USD", "1.00")
    assert _options(max_budget_usd=5.0).max_budget_usd == 5.0


def test_unparseable_env_value_falls_back_to_no_cap(monkeypatch):
    monkeypatch.setenv("MAX_BUDGET_USD", "not-a-number")
    assert _options().max_budget_usd is None
