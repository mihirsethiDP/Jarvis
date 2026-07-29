"""Gmail tools: read/search, send, and organize (archive/label/trash).

All authorized by the single `gmail.modify` scope, which is a strict
superset of read + send + label management + trash — and deliberately
excludes permanent delete and Settings changes (see docs/SECURITY.md).
"""

from __future__ import annotations

import base64
import re
from email.message import EmailMessage

from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user

_MAX_RESULTS = 20
_MAX_BODY_CHARS = 20_000
_HTML_TAG = re.compile(r"<[^>]+>")

# System labels use their literal name as the label ID; anything else must be
# resolved through users.labels.list (label IDs are per-account, not names).
_SYSTEM_LABELS = {
    "INBOX", "UNREAD", "TRASH", "STARRED", "IMPORTANT", "SENT", "DRAFT", "SPAM",
    "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_text(payload: dict) -> str:
    """Pull a readable body out of Gmail's (possibly nested multipart) MIME tree."""
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    if mime == "text/plain" and body.get("data"):
        return _decode_b64url(body["data"])

    parts = payload.get("parts") or []
    for part in parts:
        if part.get("mimeType") == "text/plain" and (part.get("body") or {}).get("data"):
            return _decode_b64url(part["body"]["data"])
    for part in parts:
        text = _extract_text(part)
        if text:
            return text
    # Only `payload` itself can still be unmatched here — any part that could
    # satisfy this check would already have been returned by the parts loop
    # above (it runs the same check on itself before returning empty).
    if mime == "text/html" and body.get("data"):
        return _HTML_TAG.sub(" ", _decode_b64url(body["data"]))
    return ""


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def build_tools(ctx: ToolContext) -> list:
    label_cache: dict[str, str] = {}  # display name -> label id, filled lazily

    def _gmail():
        return ctx.google_service("gmail", "v1")

    def _resolve_label(name: str) -> str | None:
        # A real label always wins over the system-name alias below — Gmail
        # lets an account have a custom label literally named "Trash" or
        # "Inbox", and silently treating that as the system label would
        # archive/trash a message under a confirmation that only mentioned
        # tagging it. So the account's actual labels are checked first.
        stripped = name.strip()
        if not label_cache:
            resp = _gmail().users().labels().list(userId="me").execute()
            for label in resp.get("labels", []):
                label_cache[label["name"]] = label["id"]
        if stripped in label_cache:
            return label_cache[stripped]
        # Case-insensitive fallback: voice/STT has no reliable way to
        # reproduce a label's original casing ("vendors" vs "Vendors").
        lowered = stripped.lower()
        for display_name, label_id in label_cache.items():
            if display_name.lower() == lowered:
                return label_id
        upper = stripped.upper()
        if upper in _SYSTEM_LABELS:
            return upper
        return None

    @beta_tool
    def search_email(query: str, max_results: int = 10) -> str:
        """Search the user's Gmail inbox using Gmail's search syntax
        (e.g. "from:priya", "is:unread", "subject:invoice", "newer_than:7d").

        Args:
            query: Gmail search query.
            max_results: Maximum number of results (1-20).
        """
        if not ctx.permissions.require("email_read", "search and read your Gmail"):
            return "The user declined Gmail read access."
        try:
            resp = _gmail().users().messages().list(
                userId="me", q=query, maxResults=max(1, min(int(max_results), _MAX_RESULTS)),
            ).execute()
            stubs = resp.get("messages", [])
            if not stubs:
                ctx.audit.record("tool_call", tool="search_email", detail=query)
                return f"No emails matched '{query}'."

            lines = []
            for stub in stubs:
                msg = _gmail().users().messages().get(
                    userId="me", id=stub["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                headers = msg.get("payload", {}).get("headers", [])
                lines.append(
                    f"- id: {stub['id']} | from: {_header(headers, 'From')} | "
                    f"subject: {_header(headers, 'Subject')} | "
                    f"date: {_header(headers, 'Date')} | snippet: {msg.get('snippet', '')}"
                )
            ctx.audit.record("tool_call", tool="search_email", detail=query)
            return as_document(f"gmail-search:{query}", "\n".join(lines))
        except Exception as e:
            ctx.audit.record("tool_call", tool="search_email", detail=query, ok=False)
            return f"Gmail search failed: {e}"

    @beta_tool
    def read_email(message_id: str) -> str:
        """Read the full content of one email by id (from search_email results).

        Args:
            message_id: The Gmail message id to read.
        """
        if not ctx.permissions.require("email_read", "search and read your Gmail"):
            return "The user declined Gmail read access."
        try:
            msg = _gmail().users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
            headers = msg.get("payload", {}).get("headers", [])
            body = _extract_text(msg.get("payload", {}))[:_MAX_BODY_CHARS]
            text = (
                f"From: {_header(headers, 'From')}\n"
                f"To: {_header(headers, 'To')}\n"
                f"Subject: {_header(headers, 'Subject')}\n"
                f"Date: {_header(headers, 'Date')}\n\n{body}"
            )
            ctx.audit.record("tool_call", tool="read_email", detail=message_id)
            return as_document(f"gmail:{message_id}", text)
        except Exception as e:
            ctx.audit.record("tool_call", tool="read_email", detail=message_id, ok=False)
            return f"Reading the email failed: {e}"

    @beta_tool
    def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
        """Send an email from the user's Gmail account. The user hears the
        recipient and subject and must explicitly confirm before anything is sent.

        Args:
            to: Recipient email address (comma-separate multiple addresses).
            subject: Email subject line.
            body: Plain-text body of the email.
            cc: Optional CC addresses, comma-separated.
        """
        if not ctx.permissions.require("email_send", "send email from your Gmail account"):
            return "The user declined email access."

        preview = body if len(body) <= 200 else body[:200] + "…"
        summary = (
            f"I will send an email to {to}"
            + (f" (cc {cc})" if cc else "")
            + f' with the subject "{subject}". It begins: "{preview}".'
        )
        result = ctx.confirmer.confirm(
            "send_email", summary,
            audit_detail=f'to={to} cc={cc} subject="{subject}"',  # no body in the log
        )
        if not result:
            return cancelled_by_user(result, "sending the email")

        try:
            msg = EmailMessage()
            msg["To"] = to
            if cc:
                msg["Cc"] = cc
            msg["Subject"] = subject
            msg.set_content(body)
            encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            _gmail().users().messages().send(userId="me", body={"raw": encoded}).execute()
            ctx.audit.record("tool_call", tool="send_email",
                             detail=f"to={to} subject={subject}", decision="confirmed")
            return f"Email sent to {to}."
        except Exception as e:
            ctx.audit.record("tool_call", tool="send_email", detail=f"to={to}", ok=False)
            return f"Sending failed: {e}"

    @beta_tool
    def organize_email(message_id: str, action: str, label: str = "") -> str:
        """Archive, trash, or (un)label an email. Every organize action is
        confirmed aloud before it happens, including marking read/unread —
        Jarvis never changes your mailbox silently.

        Args:
            message_id: The Gmail message id to act on.
            action: One of: archive, unarchive, trash, untrash, mark_read,
                mark_unread, add_label, remove_label.
            label: Label name — required for add_label / remove_label.
        """
        if not ctx.permissions.require("email_organize", "organize your Gmail (archive/label/trash)"):
            return "The user declined Gmail organize access."

        add: list[str] = []
        remove: list[str] = []
        summary = ""
        if action == "archive":
            remove = ["INBOX"]; summary = "archive this email"
        elif action == "unarchive":
            add = ["INBOX"]; summary = "move this email back to the inbox"
        elif action == "mark_read":
            remove = ["UNREAD"]; summary = "mark this email as read"
        elif action == "mark_unread":
            add = ["UNREAD"]; summary = "mark this email as unread"
        elif action in ("add_label", "remove_label"):
            if not label:
                return "A label name is required for add_label/remove_label."
            try:
                label_id = _resolve_label(label)
            except Exception as e:
                ctx.audit.record("tool_call", tool="organize_email",
                                 detail=f"{action} label={label} id={message_id}", ok=False)
                return f"Looking up the label '{label}' failed: {e}"
            if label_id is None:
                return f"No label named '{label}' exists in this Gmail account."
            (add if action == "add_label" else remove).append(label_id)
            summary = f"{'add' if action == 'add_label' else 'remove'} the label '{label}' " \
                      f"{'to' if action == 'add_label' else 'from'} this email"
        elif action in ("trash", "untrash"):
            summary = f"{'move this email to trash' if action == 'trash' else 'restore this email from trash'}"
        else:
            return (
                "Unknown action. Use one of: archive, unarchive, trash, untrash, "
                "mark_read, mark_unread, add_label, remove_label."
            )

        result = ctx.confirmer.confirm(
            "organize_email", f"I will {summary} (id {message_id}).",
            audit_detail=f"{action} label={label} id={message_id}",
        )
        if not result:
            return cancelled_by_user(result, "that mailbox change")

        try:
            gmail = _gmail().users().messages()
            if action == "trash":
                gmail.trash(userId="me", id=message_id).execute()
            elif action == "untrash":
                gmail.untrash(userId="me", id=message_id).execute()
            else:
                gmail.modify(
                    userId="me", id=message_id,
                    body={"addLabelIds": add, "removeLabelIds": remove},
                ).execute()
            ctx.audit.record("tool_call", tool="organize_email",
                             detail=f"{action} {message_id}", decision="confirmed")
            return f"Done — {summary}."
        except Exception as e:
            ctx.audit.record("tool_call", tool="organize_email",
                             detail=f"{action} {message_id}", ok=False)
            return f"That mailbox change failed: {e}"

    return [search_email, read_email, send_email, organize_email]
