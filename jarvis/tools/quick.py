"""Speakable answers for the intent fast-path — same gates, no model.

The fast path answers frequent read-only questions without the LLM, so it
needs answers phrased for a human ear, not for a model's context window (the
regular tools wrap content as <document> data, which is right for the model
and unspeakable aloud). These helpers exist for that: each one checks the
SAME capability permission the equivalent tool would, writes the SAME audit
event, and returns a short spoken sentence — or None, which tells the caller
to fall through to the full model.

A pleasant side effect: this path is immune to prompt injection by
construction. There is no model in the loop, so an email subject that says
"ignore your instructions" is just a subject being read aloud.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from . import ToolContext


def unread_count(ctx: ToolContext, hindi: bool) -> str | None:
    if not ctx.permissions.require("email_read", "search and read your Gmail"):
        return "आपने Gmail access मना किया है।" if hindi else "You've declined Gmail access."
    try:
        resp = ctx.google_service("gmail", "v1").users().messages().list(
            userId="me", q="is:unread in:inbox", maxResults=1,
        ).execute()
        count = int(resp.get("resultSizeEstimate", 0))
    except Exception:
        return None
    ctx.audit.record("tool_call", tool="search_email",
                     detail="fastpath: unread count")
    if count == 0:
        return "कोई unread email नहीं है।" if hindi else "No unread emails."
    if hindi:
        return f"Inbox में {count} unread email हैं।"
    plural = "email" if count == 1 else "emails"
    return f"You have {count} unread {plural}."


def latest_email(ctx: ToolContext, hindi: bool) -> str | None:
    if not ctx.permissions.require("email_read", "search and read your Gmail"):
        return "आपने Gmail access मना किया है।" if hindi else "You've declined Gmail access."
    try:
        gmail = ctx.google_service("gmail", "v1")
        stubs = gmail.users().messages().list(
            userId="me", q="in:inbox", maxResults=1).execute().get("messages", [])
        if not stubs:
            return "Inbox खाली है।" if hindi else "Your inbox is empty."
        msg = gmail.users().messages().get(
            userId="me", id=stubs[0]["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
    except Exception:
        return None
    headers = {h["name"].lower(): h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    sender = headers.get("from", "").split("<")[0].strip().strip('"') or "someone"
    subject = headers.get("subject", "(no subject)")
    snippet = " ".join((msg.get("snippet") or "").split())
    if len(snippet) > 180:
        snippet = snippet[:179] + "…"
    ctx.audit.record("tool_call", tool="read_email",
                     detail=f"fastpath: latest ({stubs[0]['id']})")
    lead = (f"सबसे नई email {sender} से है — \"{subject}\"." if hindi
            else f'Latest email is from {sender} — "{subject}".')
    return f"{lead} {snippet}" if snippet else lead


def _spoken_clock(iso: str) -> str:
    try:
        stamp = datetime.fromisoformat(iso)
        return stamp.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return iso


def events_for_day(ctx: ToolContext, day_offset: int, hindi: bool) -> str | None:
    """Today's (0) or tomorrow's (1) meetings as one spoken sentence."""
    if not ctx.permissions.require("calendar_read", "view your Google Calendar"):
        return ("आपने Calendar access मना किया है।" if hindi
                else "You've declined Calendar access.")
    day = (datetime.now().astimezone() + timedelta(days=day_offset))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.combine(day.date(), dtime(23, 59, 59), tzinfo=day.tzinfo)
    try:
        resp = ctx.google_service("calendar", "v3").events().list(
            calendarId="primary", timeMin=start.isoformat(),
            timeMax=end.isoformat(), singleEvents=True, orderBy="startTime",
            maxResults=12,
        ).execute()
    except Exception:
        return None
    label_en = "today" if day_offset == 0 else "tomorrow"
    label_hi = "आज" if day_offset == 0 else "कल"
    ctx.audit.record("tool_call", tool="list_calendar_events",
                     detail=f"fastpath: {label_en}")
    spoken: list[str] = []
    for e in resp.get("items", []):
        if e.get("status") == "cancelled":
            continue
        mine = next((a for a in e.get("attendees", []) if a.get("self")), {})
        if mine.get("responseStatus") == "declined":
            continue   # a meeting you declined is not a meeting you have
        title = e.get("summary", "(untitled)")
        start_o = e.get("start", {})
        if start_o.get("date"):
            spoken.append(f"{title} (all day)" if not hindi
                          else f"{title} (पूरे दिन)")
        else:
            spoken.append(f"{title} at {_spoken_clock(start_o.get('dateTime', ''))}"
                          if not hindi else
                          f"{title}, {_spoken_clock(start_o.get('dateTime', ''))} बजे")
    if not spoken:
        return (f"{label_hi} कोई meeting नहीं है।" if hindi
                else f"Nothing on your calendar {label_en}.")
    listing = "; ".join(spoken[:6])
    more = len(spoken) - 6
    tail = (f" — और {more} भी हैं।" if hindi else f" — and {more} more.") if more > 0 else ""
    if hindi:
        return f"{label_hi} {len(spoken)} meeting हैं: {listing}{tail}"
    plural = "meeting" if len(spoken) == 1 else "meetings"
    return f"You have {len(spoken)} {plural} {label_en}: {listing}{tail}"
