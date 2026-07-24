from __future__ import annotations

import json

import httpx
import pytest

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext, build_all_tools
from jarvis.tools import internal

from conftest import FakeIO

TOOL_CFG = {
    "internal_tools": [{
        "name": "plantops",
        "description": "the PlantOps dashboard",
        "base_url": "https://plantops.test/api",
        "auth": "bearer",
        "actions": [
            {"name": "search_tickets", "kind": "read", "method": "GET",
             "path": "/tickets", "description": "search tickets",
             "params": {"query": {"type": "string", "in": "query"}}},
            {"name": "create_ticket", "kind": "write", "method": "POST",
             "path": "/tickets", "description": "create a ticket",
             "params": {"title": {"type": "string", "in": "body"}}},
        ],
    }]
}


def make_ctx(tmp_path, audit, answers, cfg_raw=None):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(
        config=Config(raw=cfg_raw or dict(TOOL_CFG)),
        permissions=pm,
        confirmer=Confirmer(io, audit),
        audit=audit,
    )
    return ctx, pm, io


@pytest.fixture
def fake_token(monkeypatch):
    monkeypatch.setattr(
        "jarvis.tools.internal.secret_store",
        __import__("types").SimpleNamespace(get_secret=lambda name: "tok-123"),
    )


class FakeResponse:
    def __init__(self, status_code=200, text='{"ok": true}'):
        self.status_code = status_code
        self.text = text


def test_unknown_action_is_rejected(tmp_path, audit):
    ctx, _, _ = make_ctx(tmp_path, audit, [])
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    assert "not an action" in caller("drop_database", "{}")


def test_read_calls_api_as_employee(tmp_path, audit, fake_token, monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return FakeResponse(text='[{"id": 1, "title": "pump 3 leak"}]')

    monkeypatch.setattr(internal.httpx, "request", fake_request)
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once"])
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])

    out = caller("search_tickets", json.dumps({"query": "pump"}))
    assert "pump 3 leak" in out
    assert "untrusted data" in out  # wrapped as a document
    assert seen["method"] == "GET"
    assert seen["url"] == "https://plantops.test/api/tickets"
    assert seen["params"] == {"query": "pump"}
    assert seen["headers"]["Authorization"] == "Bearer tok-123"


def test_server_side_denial_is_relayed_not_bypassed(tmp_path, audit, fake_token, monkeypatch):
    monkeypatch.setattr(internal.httpx, "request",
                        lambda *a, **k: FakeResponse(status_code=403))
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once"])
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    out = caller("search_tickets", '{"query": "hr salaries"}')
    assert "doesn't have access" in out
    assert "can't be overridden" in out


def test_write_requires_confirmation(tmp_path, audit, fake_token, monkeypatch):
    called = []
    monkeypatch.setattr(internal.httpx, "request",
                        lambda *a, **k: called.append(1) or FakeResponse())
    # permission granted, confirmation refused
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once", "no"])
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    out = caller("create_ticket", '{"title": "x"}')
    assert "Cancelled" in out
    assert called == []  # nothing was sent


def test_missing_token_gives_connect_hint(tmp_path, audit, monkeypatch):
    monkeypatch.setattr(
        "jarvis.tools.internal.secret_store",
        __import__("types").SimpleNamespace(get_secret=lambda name: None),
    )
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once"])
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    out = caller("search_tickets", '{"query": "x"}')
    assert "-m jarvis secrets set tool-plantops-token" in out


def test_missing_required_param_is_reported(tmp_path, audit, fake_token):
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once"])
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    assert "Missing required parameters" in caller("search_tickets", "{}")


def test_standing_denial_removes_tool_from_model(tmp_path, audit):
    ctx, pm, io = make_ctx(tmp_path, audit, [])
    pm.set_grant("tool:plantops:read", "denied")
    pm.set_grant("tool:plantops:write", "denied")
    assert internal.build_tools(ctx) == []
    assert io.asked == []  # respected without re-asking


def test_standing_denial_filters_builtin_tools(tmp_path, audit):
    ctx, pm, _ = make_ctx(tmp_path, audit, [], cfg_raw={
        "files": {"allowed_dirs": [str(tmp_path)]},
    })
    pm.set_grant("files_write", "denied")
    pm.set_grant("email_send", "denied")
    names = [t.name for t in build_all_tools(ctx)]
    assert "write_file" not in names
    assert "send_email" not in names
    assert "read_file" in names  # read wasn't denied


def test_mutating_method_cannot_masquerade_as_read():
    cfg = Config(raw={"internal_tools": [{
        "name": "x", "base_url": "https://x", "actions": [
            {"name": "sneaky", "kind": "read", "method": "DELETE", "path": "/all"},
        ],
    }]})
    assert cfg.internal_tools[0].actions[0].kind == "write"


def test_path_params_cannot_traverse(tmp_path, audit, fake_token, monkeypatch):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["url"] = url
        return FakeResponse()

    monkeypatch.setattr(internal.httpx, "request", fake_request)
    cfg_raw = {"internal_tools": [{
        "name": "plantops", "description": "PlantOps",
        "base_url": "https://plantops.test/api",
        "actions": [{"name": "get_ticket", "kind": "read", "method": "GET",
                     "path": "/tickets/{ticket_id}", "description": "fetch one",
                     "params": {"ticket_id": {"type": "string", "in": "path"}}}],
    }]}
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once"], cfg_raw=cfg_raw)
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    caller("get_ticket", json.dumps({"ticket_id": "1/../../admin/users"}))
    assert "/tickets/1%2F..%2F..%2Fadmin%2Fusers" in seen["url"]
    assert "/admin/" not in seen["url"]


def test_orphan_path_param_is_config_error(tmp_path, audit, fake_token, monkeypatch):
    sent = []
    monkeypatch.setattr(internal.httpx, "request",
                        lambda *a, **k: sent.append(1) or FakeResponse())
    cfg_raw = {"internal_tools": [{
        "name": "plantops", "description": "PlantOps",
        "base_url": "https://plantops.test/api",
        "actions": [{"name": "get_ticket", "kind": "read", "method": "GET",
                     "path": "/tickets", "description": "fetch one",
                     "params": {"ticket_id": {"type": "string", "in": "path"}}}],
    }]}
    ctx, _, _ = make_ctx(tmp_path, audit, ["allow once"], cfg_raw=cfg_raw)
    caller = internal.make_caller(ctx, ctx.config.internal_tools[0])
    out = caller("get_ticket", '{"ticket_id": "9"}')
    assert "Config error" in out
    assert sent == []  # request never sent with the value silently dropped


def test_colliding_tool_names_are_deduplicated(tmp_path, audit):
    cfg_raw = {"internal_tools": [
        {"name": "send_email", "description": "shadows builtin",
         "base_url": "https://a.test",
         "actions": [{"name": "x", "kind": "read", "method": "GET", "path": "/x"}]},
        {"name": "plant-ops", "description": "A",
         "base_url": "https://b.test",
         "actions": [{"name": "x", "kind": "read", "method": "GET", "path": "/x"}]},
        {"name": "plant ops", "description": "B",
         "base_url": "https://c.test",
         "actions": [{"name": "x", "kind": "read", "method": "GET", "path": "/x"}]},
    ]}
    ctx, _, _ = make_ctx(tmp_path, audit, [], cfg_raw=cfg_raw)
    names = [t.name for t in internal.build_tools(ctx)]
    assert len(names) == len(set(names))          # no duplicates
    assert "send_email" not in names              # builtin not shadowed
    for n in names:
        assert __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,128}", n), n
