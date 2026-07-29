from __future__ import annotations

from jarvis.security.audit import AuditLog


def test_subscribers_see_every_recorded_event(tmp_path):
    log = AuditLog(path=tmp_path / "a.jsonl")
    seen = []
    log.subscribe(seen.append)
    log.record("permission", tool="email_read", decision="granted_once")
    log.record("tool_call", tool="search_email", detail="is:unread")
    assert [e["tool"] for e in seen] == ["email_read", "search_email"]
    assert seen[0]["decision"] == "granted_once"


def test_a_broken_subscriber_cannot_break_auditing(tmp_path):
    log = AuditLog(path=tmp_path / "a.jsonl")
    good = []
    log.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    log.subscribe(good.append)
    log.record("tool_call", tool="x")
    assert len(good) == 1                 # later subscriber still ran
    assert len(log.tail(5)) == 1          # and the event still persisted


def test_subscribers_get_a_copy_not_the_live_entry(tmp_path):
    log = AuditLog(path=tmp_path / "a.jsonl")
    captured = []
    log.subscribe(captured.append)
    log.record("tool_call", tool="x", detail="original")
    captured[0]["detail"] = "mutated"
    assert log.tail(1)[0]["detail"] == "original"


def test_state_server_renders_audit_entries_without_a_running_loop():
    from jarvis.ui.server import StateServer

    srv = StateServer(port=8799)          # never started; _send is a no-op
    srv.record_activity({
        "ts": "2026-07-29T12:00:00+00:00", "event": "tool_call",
        "tool": "send_email", "detail": "to=x@y.com", "decision": "confirmed", "ok": True,
    })
    item = srv._activity[-1]
    assert item["ts"] == "12:00:00"
    assert item["verb"] == "used"
    assert item["tool"] == "send_email"


def test_activity_feed_is_bounded():
    from jarvis.ui.server import StateServer

    srv = StateServer(port=8799)
    for i in range(500):
        srv.record_activity({"ts": "2026-07-29T12:00:00+00:00", "event": "tool_call",
                             "tool": f"t{i}", "detail": "", "decision": "", "ok": True})
    assert len(srv._activity) == 200      # oldest dropped, no unbounded growth
