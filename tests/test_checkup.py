from __future__ import annotations

from jarvis.config import Config
from jarvis.security import checkup


def _levels(findings, title_contains):
    return [f.level for f in findings if title_contains.lower() in f.title.lower()]


def test_overbroad_scopes_are_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(checkup, "_scan_for_plaintext_keys", lambda: [])
    cfg = Config(raw={"google": {"scopes": [
        "https://www.googleapis.com/auth/drive",          # full Drive
        "https://mail.google.com/",                        # full mailbox
    ]}, "files": {"allowed_dirs": [str(tmp_path)]}})
    findings = checkup.run_checks(cfg)
    assert "risk" in _levels(findings, "overbroad")


def test_least_privilege_scopes_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(checkup, "_scan_for_plaintext_keys", lambda: [])
    cfg = Config(raw={"google": {"scopes": [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/gmail.modify",
    ]}, "files": {"allowed_dirs": [str(tmp_path)]}})
    findings = checkup.run_checks(cfg)
    assert "ok" in _levels(findings, "least-privilege")


def test_plaintext_key_is_a_risk(tmp_path, monkeypatch):
    monkeypatch.setattr(checkup, "_scan_for_plaintext_keys",
                        lambda: [tmp_path / "key.txt"])
    cfg = Config(raw={"files": {"allowed_dirs": [str(tmp_path)]}})
    findings = checkup.run_checks(cfg)
    assert "risk" in _levels(findings, "plaintext")


def test_drive_root_allowlist_is_a_risk(tmp_path, monkeypatch):
    monkeypatch.setattr(checkup, "_scan_for_plaintext_keys", lambda: [])
    import pathlib
    root = pathlib.Path(tmp_path.anchor)
    cfg = Config(raw={"files": {"allowed_dirs": [str(root)]}})
    findings = checkup.run_checks(cfg)
    assert "risk" in _levels(findings, "too broad")


def test_missing_daily_limit_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(checkup, "_scan_for_plaintext_keys", lambda: [])
    cfg = Config(raw={"brain": {"daily_turn_limit": 0},
                      "files": {"allowed_dirs": [str(tmp_path)]}})
    findings = checkup.run_checks(cfg)
    assert "warn" in _levels(findings, "daily usage limit")


def test_checkup_never_prompts(tmp_path, monkeypatch):
    # The silent IO must raise rather than block a scheduled run.
    import pytest
    monkeypatch.setattr(checkup, "_scan_for_plaintext_keys", lambda: [])
    with pytest.raises(EOFError):
        checkup._SilentIO().ask("anything?")
