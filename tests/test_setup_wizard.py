from __future__ import annotations

from jarvis.config import Config
from jarvis.security.permissions import PermissionManager
from jarvis.setup_wizard import capability_specs, run_setup

from conftest import FakeIO


def test_specs_cover_configured_tools():
    cfg = Config(raw={
        "ai_tools": [{"name": "company-gpt", "base_url": "http://x", "model": "m"}],
        "internal_tools": [{
            "name": "plantops", "base_url": "http://x",
            "actions": [{"name": "a", "kind": "write", "method": "POST", "path": "/a"}],
        }],
    })
    caps = [c for c, _, _ in capability_specs(cfg)]
    assert "files_read" in caps and "email_send" in caps
    assert "ai:company-gpt" in caps
    assert "tool:plantops:read" in caps and "tool:plantops:write" in caps


def test_wizard_records_decisions(tmp_path, audit, monkeypatch):
    monkeypatch.setattr("jarvis.setup_wizard.app_data_dir", lambda: tmp_path)
    cfg = Config(raw={})
    # 5 built-in capabilities: allow, deny, ask, gibberish-then-deny, allow
    io = FakeIO(["allow", "deny", "", "whatever", "deny", "a"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")

    decisions = run_setup(cfg, io=io, pm=pm)

    assert decisions["files_read"] == "always"
    assert decisions["files_write"] == "denied"
    assert decisions["drive_read"] == "ask"
    assert decisions["drive_write"] == "denied"   # gibberish re-asked, then denied
    assert decisions["email_send"] == "always"
    assert (tmp_path / "setup_done").exists()

    # Standing decisions behave correctly at runtime.
    assert pm.granted("files_read") is True
    assert pm.denied("files_write") is True
    assert pm.require("files_write", "write") is False  # no prompt, fails closed


def test_denied_capability_never_prompts_again(tmp_path, audit):
    io = FakeIO([])  # would raise if asked
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    pm.set_grant("drive_read", "denied")
    assert pm.require("drive_read", "read Drive") is False
    assert io.asked == []


def test_ask_scope_clears_previous_decision(tmp_path, audit):
    io = FakeIO(["allow once"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    pm.set_grant("drive_read", "denied")
    pm.set_grant("drive_read", "ask")
    assert pm.require("drive_read", "read Drive") is True  # prompted again
    assert len(io.asked) == 1


def test_eof_aborts_wizard_without_marker(tmp_path, audit, monkeypatch):
    monkeypatch.setattr("jarvis.setup_wizard.app_data_dir", lambda: tmp_path)
    cfg = Config(raw={})
    io = FakeIO(["allow"])  # answers run out after the first capability -> EOF
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    decisions = run_setup(cfg, io=io, pm=pm)
    assert decisions == {"files_read": "always"}     # partial only
    assert not (tmp_path / "setup_done").exists()    # not marked complete


def test_bare_yes_is_not_accepted(tmp_path, audit, monkeypatch):
    monkeypatch.setattr("jarvis.setup_wizard.app_data_dir", lambda: tmp_path)
    cfg = Config(raw={})
    # "yes" must re-prompt; then explicit answers finish the wizard.
    io = FakeIO(["yes", "allow", "ask", "ask", "ask", "deny"])
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    decisions = run_setup(cfg, io=io, pm=pm)
    assert decisions["files_read"] == "always"
    assert decisions["email_send"] == "denied"
