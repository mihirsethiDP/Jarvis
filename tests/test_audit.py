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
