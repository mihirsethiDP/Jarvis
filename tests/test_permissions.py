from __future__ import annotations

from jarvis.security.permissions import PermissionManager

from conftest import FakeIO


def make_pm(tmp_path, audit, answers):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json",
                           session_grant_minutes=60)
    return pm, io


def test_deny_by_default_on_silence(tmp_path, audit):
    pm, _ = make_pm(tmp_path, audit, [""])
    assert pm.require("drive_read", "read Drive") is False


def test_gibberish_fails_closed(tmp_path, audit):
    pm, _ = make_pm(tmp_path, audit, ["purple monkey dishwasher"])
    assert pm.require("email_send", "send email") is False


def test_allow_once_does_not_persist(tmp_path, audit):
    pm, io = make_pm(tmp_path, audit, ["allow once", ""])
    assert pm.require("files_read", "read files") is True
    # Second call must ask again (and the empty answer denies).
    assert pm.require("files_read", "read files") is False
    assert len(io.asked) == 2


def test_session_grant_skips_prompt(tmp_path, audit):
    pm, io = make_pm(tmp_path, audit, ["allow for this session"])
    assert pm.require("files_read", "read files") is True
    assert pm.require("files_read", "read files") is True
    assert len(io.asked) == 1


def test_always_grant_persists_across_instances(tmp_path, audit):
    pm, _ = make_pm(tmp_path, audit, ["always allow"])
    assert pm.require("drive_read", "read Drive") is True

    pm2, io2 = make_pm(tmp_path, audit, [])
    assert pm2.require("drive_read", "read Drive") is True
    assert io2.asked == []


def test_revoke_removes_persistent_grant(tmp_path, audit):
    pm, _ = make_pm(tmp_path, audit, ["always allow"])
    pm.require("drive_read", "read Drive")
    assert pm.revoke("drive_read") is True

    pm2, _ = make_pm(tmp_path, audit, [""])
    assert pm2.require("drive_read", "read Drive") is False
