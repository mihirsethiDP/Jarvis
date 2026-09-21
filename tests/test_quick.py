"""Speakable fast-path answers: same gates as the tools, phrased for the ear."""

from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.memory import MemoryStore
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext, quick

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, service):
    io = FakeIO(answers)
    ctx = ToolContext(config=Config(raw={}),
                      permissions=PermissionManager(io, audit, store_path=tmp_path / "p.json"),
                      confirmer=Confirmer(io, audit), audit=audit,
                      memory=MemoryStore(path=tmp_path / "m.json"))
    ctx.google_service = lambda api, version: service
    return ctx


def test_unread_count_speaks_a_number(tmp_path, audit):
    service = MagicMock()
    service.users().messages().list.return_value.execute.return_value = {
        "resultSizeEstimate": 7}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    assert quick.unread_count(ctx, hindi=False) == "You have 7 unread emails."
    assert "7" in quick.unread_count(ctx, hindi=True)


def test_permission_denied_answers_instead_of_guessing(tmp_path, audit):
    ctx = make_ctx(tmp_path, audit, ["deny always"], MagicMock())
    out = quick.unread_count(ctx, hindi=False)
    assert out == "You've declined Gmail access."


def test_api_failure_falls_through_to_the_model(tmp_path, audit):
    service = MagicMock()
    service.users().messages().list.return_value.execute.side_effect = RuntimeError
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    assert quick.unread_count(ctx, hindi=False) is None


def test_latest_email_reads_sender_subject_snippet(tmp_path, audit):
    service = MagicMock()
    service.users().messages().list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]}
    service.users().messages().get.return_value.execute.return_value = {
        "payload": {"headers": [
            {"name": "From", "value": '"Ranjana Majumdar" <r@digitalpaani.com>'},
            {"name": "Subject", "value": "Q3 report"},
        ]},
        "snippet": "Hi Mihir, sharing the Q3 numbers before the review",
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    out = quick.latest_email(ctx, hindi=False)
    assert "Ranjana Majumdar" in out and "Q3 report" in out
    assert "Q3 numbers" in out
    assert "<r@digitalpaani.com>" not in out, "no email addresses read aloud"


def test_calendar_peek_skips_declined_and_speaks_times(tmp_path, audit):
    service = MagicMock()
    service.events().list.return_value.execute.return_value = {"items": [
        {"summary": "Standup",
         "start": {"dateTime": "2026-09-22T10:00:00+05:30"},
         "attendees": [{"self": True, "responseStatus": "accepted"}]},
        {"summary": "Vendor call",
         "start": {"dateTime": "2026-09-22T15:30:00+05:30"},
         "attendees": [{"self": True, "responseStatus": "declined"}]},
        {"summary": "Site visit", "start": {"date": "2026-09-22"}},
    ]}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    out = quick.events_for_day(ctx, 1, hindi=False)
    assert out.startswith("You have 2 meetings tomorrow")
    assert "Standup at 10:00 AM" in out
    assert "Site visit (all day)" in out
    assert "Vendor call" not in out, "a declined meeting is not a meeting you have"


def test_empty_day_says_so(tmp_path, audit):
    service = MagicMock()
    service.events().list.return_value.execute.return_value = {"items": []}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    assert quick.events_for_day(ctx, 0, hindi=False) == "Nothing on your calendar today."
    assert quick.events_for_day(ctx, 0, hindi=True) == "आज कोई meeting नहीं है।"
