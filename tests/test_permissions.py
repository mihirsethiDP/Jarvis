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
    pm, io = make_pm(tmp_path, audit, ["allow once", "", ""])
    assert pm.require("files_read", "read files") is True
    # Second call must ask again, and silence still denies — but silence now
    # gets one repeat first, because a mis-transcribed answer was previously
    # indistinguishable from a refusal.
    assert pm.require("files_read", "read files") is False
    assert len(io.asked) == 3
    assert "didn't catch that" in io.asked[-1]


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


def test_spoken_answers_with_punctuation_are_understood(tmp_path, audit):
    # Whisper transcribes speech with punctuation/capitalization.
    pm, _ = make_pm(tmp_path, audit, ["Allow once."])
    assert pm.require("files_read", "read files") is True

    pm2, _ = make_pm(tmp_path, audit, ["Always allow!"])
    assert pm2.require("drive_read", "read Drive") is True


def test_revoke_removes_persistent_grant(tmp_path, audit):
    pm, _ = make_pm(tmp_path, audit, ["always allow"])
    pm.require("drive_read", "read Drive")
    assert pm.revoke("drive_read") is True

    pm2, _ = make_pm(tmp_path, audit, [""])
    assert pm2.require("drive_read", "read Drive") is False


def test_weak_hindi_fillers_do_not_grant(tmp_path, audit):
    # "ji" / "ek baar" are ambient office speech, not consent.
    for answer in ["ji", "Ji.", "जी", "ek baar", "एक बार।"]:
        pm, _ = make_pm(tmp_path, audit, [answer])
        assert pm.require("files_read", "read files") is False, answer


def test_unambiguous_hindi_answers_grant(tmp_path, audit):
    for answer in ["haan", "ji haan", "Theek hai."]:
        pm, _ = make_pm(tmp_path, audit, [answer])
        assert pm.require("files_read", "read files") is True, answer


def test_eof_during_prompt_fails_closed(tmp_path, audit):
    pm, _ = make_pm(tmp_path, audit, [])  # FakeIO raises EOFError
    assert pm.require("files_read", "read files") is False
