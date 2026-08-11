"""Google Calendar tools: view events, check free/busy, create/update/delete events.

calendar.readonly alone authorizes freebusy.query (no separate freebusy scope
needed). Free/busy only ever returns busy intervals, never event details, so
checking a colleague's availability cannot leak what a meeting is about.
"""

from __future__ import annotations

from datetime import date, timedelta

from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user

_MAX_EVENTS = 25


def _inclusive_end(exclusive_date: str) -> str:
    """Google ends all-day events on the day AFTER they finish."""
    try:
        return (date.fromisoformat(exclusive_date) - timedelta(days=1)).isoformat()
    except ValueError:
        return exclusive_date


def build_tools(ctx: ToolContext) -> list:
    def _describe_event(calendar_id: str, event_id: str) -> str:
        """Title and start time, so the user knows which meeting is going."""
        try:
            ev = _calendar().events().get(
                calendarId=calendar_id, eventId=event_id).execute()
            title = ev.get("summary") or "(untitled event)"
            start = ev.get("start", {})
            when = start.get("dateTime") or start.get("date") or ""
            when = when.replace("T", " ")[:16]
            return f'"{title}"' + (f" on {when}" if when else "")
        except Exception:
            return f"the event with id {event_id}"

    def _calendar():
        return ctx.google_service("calendar", "v3")

    @beta_tool
    def list_calendar_events(time_min: str, time_max: str, calendar_id: str = "primary") -> str:
        """List calendar events in a time range.

        Args:
            time_min: Start of the range, RFC3339 (e.g. "2026-07-28T00:00:00+05:30").
            time_max: End of the range, RFC3339.
            calendar_id: Calendar to read — "primary" for the user's own calendar,
                or a colleague's calendar id/email if it's shared with them.
        """
        if not ctx.permissions.require("calendar_read", "view your Google Calendar"):
            return "The user declined Calendar read access."
        try:
            resp = _calendar().events().list(
                calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime", maxResults=_MAX_EVENTS,
            ).execute()
            events = resp.get("items", [])
            ctx.audit.record("tool_call", tool="list_calendar_events",
                             detail=f"{calendar_id} {time_min}..{time_max}")
            if not events:
                return f"No events on {calendar_id} between {time_min} and {time_max}."
            lines = []
            for e in events:
                start_o, end_o = e.get("start", {}), e.get("end", {})
                if start_o.get("date"):
                    # All-day events use an EXCLUSIVE end date, so a one-day
                    # event reads as spanning two days unless it is adjusted.
                    start = start_o["date"]
                    end = _inclusive_end(end_o.get("date", start))
                    when = f"all day on {start}" if end == start else f"all day {start} to {end}"
                else:
                    when = f"{start_o.get('dateTime', '?')} to {end_o.get('dateTime', '?')}"
                attendees = ", ".join(a.get("email", "") for a in e.get("attendees", []))
                # A meeting the user already declined is not a meeting they
                # have. Reporting it as one made the day look busier than it is.
                mine = next((a for a in e.get("attendees", []) if a.get("self")), {})
                status = mine.get("responseStatus", "")
                note = {"declined": "  [you declined this]",
                        "tentative": "  [you marked this tentative]",
                        "needsAction": "  [you have not responded]"}.get(status, "")
                if e.get("status") == "cancelled":
                    note = "  [cancelled]"
                lines.append(
                    f"- {e.get('summary', '(no title)')}: {when}"
                    f"  (id: {e['id']})" + (f"  attendees: {attendees}" if attendees else "")
                    + note
                )
            return as_document(f"calendar:{calendar_id}", "\n".join(lines))
        except Exception as e:
            ctx.audit.record("tool_call", tool="list_calendar_events", ok=False)
            return f"Listing calendar events failed: {e}"

    @beta_tool
    def check_availability(time_min: str, time_max: str, people: str) -> str:
        """Check free/busy for one or more people in a time range. Only shows
        busy/free intervals, never event titles or details — safe to use for
        a colleague's calendar without exposing what their meetings are about.

        Args:
            time_min: Start of the range, RFC3339.
            time_max: End of the range, RFC3339.
            people: Comma-separated email addresses to check (include your own
                "primary" or your email to check yourself).
        """
        if not ctx.permissions.require("calendar_read", "view your Google Calendar"):
            return "The user declined Calendar read access."
        ids = [p.strip() for p in people.split(",") if p.strip()]
        if not ids:
            return "Provide at least one email address to check."
        try:
            resp = _calendar().freebusy().query(body={
                "timeMin": time_min, "timeMax": time_max,
                "items": [{"id": i} for i in ids],
            }).execute()
            ctx.audit.record("tool_call", tool="check_availability", detail=",".join(ids))
            lines = []
            for cal_id, info in resp.get("calendars", {}).items():
                if info.get("errors"):
                    # Never coerce an access error into "free" — surface as unknown.
                    lines.append(f"- {cal_id}: unknown (no access to this calendar)")
                    continue
                busy = info.get("busy", [])
                if not busy:
                    lines.append(f"- {cal_id}: free for the whole range")
                else:
                    intervals = "; ".join(f"{b['start']}–{b['end']}" for b in busy)
                    lines.append(f"- {cal_id}: busy {intervals}")
            return as_document("calendar-freebusy", "\n".join(lines))
        except Exception as e:
            ctx.audit.record("tool_call", tool="check_availability", ok=False)
            return f"Checking availability failed: {e}"

    @beta_tool
    def create_calendar_event(
        summary: str, start: str, end: str, attendees: str = "",
        description: str = "", calendar_id: str = "primary",
    ) -> str:
        """Create a calendar event. Every attendee gets an email invite, so
        this is always confirmed aloud before it's created.

        Args:
            summary: Event title.
            start: Start time, RFC3339 (e.g. "2026-07-28T15:00:00+05:30").
            end: End time, RFC3339.
            attendees: Comma-separated attendee email addresses.
            description: Optional event description.
            calendar_id: Calendar to create it on (usually "primary").
        """
        if not ctx.permissions.require("calendar_write", "create or change your Calendar events"):
            return "The user declined Calendar write access."
        attendee_list = [a.strip() for a in attendees.split(",") if a.strip()]
        summary_text = (
            f'I will create "{summary}" from {start} to {end}'
            + (f", inviting {', '.join(attendee_list)}" if attendee_list else "")
            + "."
        )
        if description:
            # Attendees get this emailed to them — read it back like any other
            # outgoing content (matches send_email/send_chat_message previews).
            preview = description if len(description) <= 200 else description[:200] + "…"
            summary_text += f' Description: "{preview}".'
        result = ctx.confirmer.confirm(
            "create_calendar_event", summary_text,
            audit_detail=f"{summary} {start}..{end} attendees={len(attendee_list)}",
        )
        if not result:
            return cancelled_by_user(result, "creating that event")
        try:
            body = {
                "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end},
            }
            if description:
                body["description"] = description
            if attendee_list:
                body["attendees"] = [{"email": a} for a in attendee_list]
            created = _calendar().events().insert(
                calendarId=calendar_id, body=body,
                sendUpdates="all" if attendee_list else "none",
            ).execute()
            ctx.audit.record("tool_call", tool="create_calendar_event",
                             detail=summary, decision="confirmed")
            return f"Created '{summary}': {created.get('htmlLink', created['id'])}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="create_calendar_event",
                             detail=summary, ok=False)
            return f"Creating the event failed: {e}"

    @beta_tool
    def delete_calendar_event(event_id: str, calendar_id: str = "primary") -> str:
        """Delete a calendar event. Attendees get a cancellation email, so
        this is confirmed aloud before it happens.

        Args:
            event_id: The event id to delete (from list_calendar_events).
            calendar_id: Calendar the event is on (usually "primary").
        """
        if not ctx.permissions.require("calendar_write", "create or change your Calendar events"):
            return "The user declined Calendar write access."
        # Cancelling a meeting emails every attendee. Confirming against an
        # opaque event id gave the user no way to tell which meeting.
        result = ctx.confirmer.confirm(
            "delete_calendar_event",
            f"I will cancel {_describe_event(calendar_id, event_id)} and notify attendees.",
            audit_detail=event_id,
        )
        if not result:
            return cancelled_by_user(result, "deleting that event")
        try:
            _calendar().events().delete(
                calendarId=calendar_id, eventId=event_id, sendUpdates="all"
            ).execute()
            ctx.audit.record("tool_call", tool="delete_calendar_event",
                             detail=event_id, decision="confirmed")
            return "Event deleted."
        except Exception as e:
            ctx.audit.record("tool_call", tool="delete_calendar_event",
                             detail=event_id, ok=False)
            return f"Deleting the event failed: {e}"

    return [list_calendar_events, check_availability, create_calendar_event, delete_calendar_event]
