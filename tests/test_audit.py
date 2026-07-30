from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

from jarvis.security.audit import AuditLog


def test_records_are_appended_with_fields(tmp_path):
    log = AuditLog(path=tmp_path / "a.jsonl")
    log.record("tool_call", tool="read_file", detail="C:/x.txt", decision="allowed")
    log.record("permission", tool="drive_read", decision="denied", ok=False)

    entries = log.tail(10)
    assert len(entries) == 2
    assert entries[0]["tool"] == "read_file"
    assert entries[1]["decision"] == "denied"
    assert all("ts" in e and "hash" in e and "prev_hash" in e for e in entries)


def test_chain_verifies_intact(tmp_path):
    log = AuditLog(path=tmp_path / "a.jsonl")
    for i in range(5):
        log.record("tool_call", tool=f"t{i}")
    intact, count = log.verify_chain()
    assert intact is True
    assert count == 5


def test_tampering_breaks_chain(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path=path)
    for i in range(3):
        log.record("tool_call", tool=f"t{i}")

    lines = path.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[1])
    doctored["tool"] = "something_else"
    lines[1] = json.dumps(doctored, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    intact, _ = AuditLog(path=path).verify_chain()
    assert intact is False


def test_chain_continues_across_instances(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path=path).record("startup")
    AuditLog(path=path).record("tool_call", tool="x")
    intact, count = AuditLog(path=path).verify_chain()
    assert intact is True
    assert count == 2


def test_interleaved_writers_extend_one_chain(tmp_path):
    # Assistant + CLI appending concurrently must not fork the chain.
    path = tmp_path / "a.jsonl"
    writer_a, writer_b = AuditLog(path=path), AuditLog(path=path)
    for i in range(3):
        writer_a.record("tool_call", tool=f"a{i}")
        writer_b.record("tool_call", tool=f"b{i}")
    intact, count = AuditLog(path=path).verify_chain()
    assert intact is True
    assert count == 6


def test_anchor_detects_tail_truncation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store: dict[str, str] = {}
    fake = SimpleNamespace(
        get_secret=store.get,
        set_secret=lambda name, value: store.__setitem__(name, value) or True,
        delete_secret=lambda name: store.pop(name, None) is not None,
    )
    monkeypatch.setattr("jarvis.security.audit.secret_store", fake)

    path = tmp_path / "a.jsonl"
    log = AuditLog(path=path, anchored=True)
    for i in range(4):
        log.record("tool_call", tool=f"t{i}")
    assert AuditLog(path=path, anchored=True).verify_chain() == (True, 4)

    # Remove the newest entry — a bare hash chain cannot see this.
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    intact, _ = AuditLog(path=path, anchored=True).verify_chain()
    assert intact is False


def _fake_keyring(monkeypatch):
    """Isolate the anchor from the real Windows keyring."""
    store: dict[str, str] = {}
    fake = SimpleNamespace(
        get_secret=store.get,
        set_secret=lambda name, value: store.__setitem__(name, value) or True,
        delete_secret=lambda name: store.pop(name, None) is not None,
    )
    monkeypatch.setattr("jarvis.security.audit.secret_store", fake)
    return store


def test_dropped_write_does_not_advance_the_anchor(tmp_path, monkeypatch, capsys):
    # Observed for real: the filesystem accepted 19 appends that never landed,
    # while the anchor counted every one. The anchor then permanently
    # disagreed with an untouched file, and verification called it tampering.
    _fake_keyring(monkeypatch)
    path = tmp_path / "a.jsonl"
    log = AuditLog(path=path, anchored=True)
    log.record("startup")
    before = log._load_anchor()

    # First call supplies the prev_hash; the second is the read-back that
    # proves the entry landed. Returning the wrong hash there is exactly what
    # a silently-discarded write looks like.
    with mock.patch.object(AuditLog, "_read_tail_hash",
                           side_effect=[before["head"], "not-the-entry-hash"]):
        log.record("permission", tool="cap1", decision="setup_always")

    assert log._load_anchor() == before, "anchor advanced on a write that never landed"
    assert "SECURITY WARNING" in capsys.readouterr().err


def test_verify_separates_missing_entries_from_edited_ones(tmp_path, monkeypatch):
    _fake_keyring(monkeypatch)
    path = tmp_path / "a.jsonl"
    log = AuditLog(path=path, anchored=True)
    for i in range(4):
        log.record("tool_call", tool=f"t{i}")

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    intact, count, reason, missing = AuditLog(path=path, anchored=True).verify()
    assert (intact, count, reason, missing) == (False, 3, "entries_missing", 1)

    # An edited entry is a different incident and must not be conflated.
    edited = json.loads(lines[1])
    edited["detail"] = "tampered"
    lines[1] = json.dumps(edited, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, _, reason, _ = AuditLog(path=path, anchored=True).verify()
    assert reason == "entry_modified"


def test_reanchor_refuses_an_edited_log(tmp_path, monkeypatch):
    _fake_keyring(monkeypatch)
    path = tmp_path / "a.jsonl"
    log = AuditLog(path=path, anchored=True)
    for i in range(3):
        log.record("tool_call", tool=f"t{i}")

    lines = path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[0])
    edited["detail"] = "tampered"
    lines[0] = json.dumps(edited, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, message = AuditLog(path=path, anchored=True).reanchor()
    assert ok is False
    assert "Refusing to re-anchor" in message


def test_reanchor_records_the_gap_it_forgives(tmp_path, monkeypatch):
    _fake_keyring(monkeypatch)
    path = tmp_path / "a.jsonl"
    log = AuditLog(path=path, anchored=True)
    for i in range(4):
        log.record("tool_call", tool=f"t{i}")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")

    fresh = AuditLog(path=path, anchored=True)
    ok, message = fresh.reanchor()
    assert ok is True
    assert "2 entry(ies)" in message
    assert fresh.verify()[0] is True          # verifies again afterwards
    entries = fresh.tail(1)
    assert entries[0]["tool"] == "reanchor"   # the gap stays visible in the log
    assert entries[0]["ok"] is False
