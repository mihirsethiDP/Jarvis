from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import claude_bridge
from jarvis.usage import TurnBudget

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, budget=None):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    return ToolContext(config=Config(raw={}), permissions=pm,
                       confirmer=Confirmer(io, audit), audit=audit, turn_budget=budget)


def _fake_client(monkeypatch, text="drafted reply"):
    block = MagicMock(); block.type = "text"; block.text = text
    response = MagicMock(); response.content = [block]
    client = MagicMock()
    client.messages.create.return_value = response
    monkeypatch.setattr(claude_bridge.anthropic, "Anthropic", lambda *a, **k: client)
    return client


def test_answer_comes_back_wrapped_as_untrusted_data(tmp_path, audit, monkeypatch):
    _fake_client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in claude_bridge.build_tools(ctx)}["ask_claude"]("draft a note")
    assert "drafted reply" in out
    assert "untrusted data" in out


def test_no_tools_are_ever_passed_to_the_subcall(tmp_path, audit, monkeypatch):
    client = _fake_client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    {t.name: t for t in claude_bridge.build_tools(ctx)}["ask_claude"]("hi")
    _, kwargs = client.messages.create.call_args
    assert "tools" not in kwargs          # no file/shell/API reach
    assert len(kwargs["messages"]) == 1   # fresh context, no history


def test_denied_permission_makes_no_api_call(tmp_path, audit, monkeypatch):
    client = _fake_client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, ["deny"])
    out = {t.name: t for t in claude_bridge.build_tools(ctx)}["ask_claude"]("hi")
    assert "declined" in out
    client.messages.create.assert_not_called()


def test_counts_against_the_daily_budget(tmp_path, audit, monkeypatch):
    client = _fake_client(monkeypatch)
    budget = TurnBudget(1, path=tmp_path / "u.json", today=lambda: "2026-07-29")
    ctx = make_ctx(tmp_path, audit, ["allow once", "allow once"], budget=budget)
    tool = {t.name: t for t in claude_bridge.build_tools(ctx)}["ask_claude"]
    tool("first")
    assert budget.used_today() == 1
    out = tool("second")                  # over the limit
    assert "usage limit" in out
    assert client.messages.create.call_count == 1


def test_empty_prompt_is_rejected(tmp_path, audit, monkeypatch):
    client = _fake_client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, [])
    out = {t.name: t for t in claude_bridge.build_tools(ctx)}["ask_claude"]("   ")
    assert "empty" in out
    client.messages.create.assert_not_called()
