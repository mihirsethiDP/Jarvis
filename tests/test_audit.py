from __future__ import annotations

import json

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
