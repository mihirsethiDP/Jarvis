from __future__ import annotations

from unittest.mock import MagicMock

from jarvis.config import Config
from jarvis.security.confirm import Confirmer
from jarvis.security.permissions import PermissionManager
from jarvis.tools import ToolContext
from jarvis.tools import gcalendar as gcal_mod

from conftest import FakeIO


def make_ctx(tmp_path, audit, answers, fake_service):
    io = FakeIO(answers)
    pm = PermissionManager(io, audit, store_path=tmp_path / "perms.json")
    ctx = ToolContext(config=Config(raw={}), permissions=pm,
                      confirmer=Confirmer(io, audit), audit=audit)
    ctx.google_service = lambda api, version: fake_service
    return ctx


def test_list_events_uses_single_events_and_start_time_order(tmp_path, audit):
    service = MagicMock()
    service.events().list.return_value.execute.return_value = {"items": []}
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gcal_mod.build_tools(ctx)}
    tools["list_calendar_events"]("2026-07-28T00:00:00+05:30", "2026-07-29T00:00:00+05:30")
    service.events().list.assert_called_with(
        calendarId="primary", timeMin="2026-07-28T00:00:00+05:30",
        timeMax="2026-07-29T00:00:00+05:30", singleEvents=True,
        orderBy="startTime", maxResults=25,
    )


def test_freebusy_access_error_is_unknown_not_free(tmp_path, audit):
    service = MagicMock()
    service.freebusy().query.return_value.execute.return_value = {
        "calendars": {
            "me@x.com": {"busy": [{"start": "t1", "end": "t2"}]},
            "colleague@x.com": {"errors": [{"reason": "notFound"}]},
        }
    }
    ctx = make_ctx(tmp_path, audit, ["allow once"], service)
    tools = {t.name: t for t in gcal_mod.build_tools(ctx)}
    out = tools["check_availability"]("t1", "t2", "me@x.com, colleague@x.com")
    lines = {line.split(":")[0].strip("- "): line for line in out.splitlines() if ":" in line}
    assert "busy" in lines["me@x.com"]
    assert "unknown" in lines["colleague@x.com"]
    assert "free" not in lines["colleague@x.com"]


def test_create_event_confirms_and_sends_updates_when_attendees(tmp_path, audit):
    service = MagicMock()
    service.events().insert.return_value.execute.return_value = {"id": "e1", "htmlLink": "http://x"}
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gcal_mod.build_tools(ctx)}
    out = tools["create_calendar_event"](
        "Sync", "2026-07-28T15:00:00+05:30", "2026-07-28T15:30:00+05:30",
        attendees="colleague@x.com",
    )
    assert "Created" in out
    _, kwargs = service.events().insert.call_args
    assert kwargs["sendUpdates"] == "all"
    assert kwargs["body"]["attendees"] == [{"email": "colleague@x.com"}]


def test_create_event_without_attendees_skips_updates(tmp_path, audit):
    service = MagicMock()
    service.events().insert.return_value.execute.return_value = {"id": "e1"}
    ctx = make_ctx(tmp_path, audit, ["allow once", "yes"], service)
    tools = {t.name: t for t in gcal_mod.build_tools(ctx)}
    tools["create_calendar_event"]("Focus block", "t1", "t2")
    _, kwargs = service.events().insert.call_args
    assert kwargs["sendUpdates"] == "none"


def test_delete_event_confirms_before_deleting(tmp_path, audit):
    service = MagicMock()
    ctx = make_ctx(tmp_path, audit, ["allow once", "no"], service)
    tools = {t.name: t for t in gcal_mod.build_tools(ctx)}
    out = tools["delete_calendar_event"]("e1")
    assert "Cancelled" in out
    service.events().delete.assert_not_called()
