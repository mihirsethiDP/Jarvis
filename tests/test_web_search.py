from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import web_search as ws
from jarvis.usage import TurnBudget

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, budget=None):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    return ToolContext(config=Config(raw={}), permissions=pm,
                       confirmer=Confirmer(io, audit), audit=audit, turn_budget=budget)


def _client(monkeypatch, *, text="Recent news summary.", results=None, stop="end_turn"):
    blocks = []
    tb = MagicMock(); tb.type = "text"; tb.text = text
    blocks.append(tb)
    if results is not None:
        rb = MagicMock(); rb.type = "web_search_tool_result"; rb.content = results
        blocks.append(rb)
    response = MagicMock(); response.content = blocks; response.stop_reason = stop
    client = MagicMock(); client.messages.create.return_value = response
    monkeypatch.setattr(ws.anthropic, "Anthropic", lambda *a, **k: client)
    return client


def _result(title, url):
    item = MagicMock(); item.title = title; item.url = url
    return item


def test_answer_and_sources_come_back_wrapped(tmp_path, audit, monkeypatch):
    _client(monkeypatch, results=[_result("Reuters piece", "https://reuters.com/x")])
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in ws.build_tools(ctx)}["search_web"]("what happened today")
    assert "Recent news summary." in out
    assert "https://reuters.com/x" in out      # attributable
    assert "untrusted data" in out              # internet content is not trusted


def test_uses_the_server_side_search_tool(tmp_path, audit, monkeypatch):
    client = _client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    {t.name: t for t in ws.build_tools(ctx)}["search_web"]("q")
    _, kwargs = client.messages.create.call_args
    tool = kwargs["tools"][0]
    assert tool["name"] == "web_search"
    assert tool["type"].startswith("web_search_")
    assert tool["max_uses"] == ws._MAX_SEARCHES     # bounded, can't run away


def test_error_object_instead_of_result_list_is_survivable(tmp_path, audit, monkeypatch):
    # Server tools report failure as an error OBJECT in content, not a list.
    err = MagicMock(); err.error_code = "max_uses_exceeded"
    _client(monkeypatch, results=err)
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    out = {t.name: t for t in ws.build_tools(ctx)}["search_web"]("q")
    assert "Recent news summary." in out         # the answer still comes through


def test_denied_permission_makes_no_api_call(tmp_path, audit, monkeypatch):
    client = _client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, ["deny"])
    out = {t.name: t for t in ws.build_tools(ctx)}["search_web"]("q")
    assert "declined" in out
    client.messages.create.assert_not_called()


def test_counts_against_the_daily_budget(tmp_path, audit, monkeypatch):
    client = _client(monkeypatch)
    budget = TurnBudget(1, path=tmp_path / "u.json", today=lambda: "2026-07-30")
    ctx = make_ctx(tmp_path, audit, ["allow once", "allow once"], budget=budget)
    tool = {t.name: t for t in ws.build_tools(ctx)}["search_web"]
    tool("first")
    out = tool("second")
    assert "usage limit" in out
    assert client.messages.create.call_count == 1


def test_refusal_is_relayed(tmp_path, audit, monkeypatch):
    _client(monkeypatch, stop="refusal")
    ctx = make_ctx(tmp_path, audit, ["allow once"])
    assert "can't search" in {t.name: t for t in ws.build_tools(ctx)}["search_web"]("q")


def test_empty_query_is_rejected(tmp_path, audit, monkeypatch):
    client = _client(monkeypatch)
    ctx = make_ctx(tmp_path, audit, [])
    assert "What should I search" in {t.name: t for t in ws.build_tools(ctx)}["search_web"]("  ")
    client.messages.create.assert_not_called()
