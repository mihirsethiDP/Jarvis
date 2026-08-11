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
# <style> and <script> bodies are not markup, so stripping tags alone left
# their CONTENTS behind — Jarvis read stylesheet rules and JavaScript aloud
# from any marketing email. Removed whole, before tags.
_HTML_DROP_BLOCKS = re.compile(
    r"<(script|style|head)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HTML_BREAKS = re.compile(r"(?i)<(br\s*/?|/p|/div|/tr|/h[1-6])>")


def _html_to_text(html: str) -> str:
    """Readable text from an HTML email body."""
    import html as _html

    text = _HTML_DROP_BLOCKS.sub(" ", html)
    text = _HTML_BREAKS.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    # &nbsp; and friends were read out literally as "and n b s p".
    text = _html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()

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
        return _html_to_text(_decode_b64url(body["data"]))
    return ""


def _attachments(payload: dict) -> list[str]:
    """Names and sizes of any attached files.

    read_email never mentioned attachments at all, so an email whose whole
    point was the file it carried came back looking like a bare note — and
    Jarvis would report there was nothing attached.
    """
    found: list[str] = []

    def walk(part: dict) -> None:
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        if filename and body.get("attachmentId"):
            size = body.get("size") or 0
            found.append(f"{filename} ({max(1, size // 1024)} KB)")
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return found


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def build_tools(ctx: ToolContext) -> list:
    def _describe_message(message_id: str) -> str:
        """Sender and subject for a confirmation the user can actually check."""
        try:
            meta = _gmail().users().messages().get(
                userId="me", id=message_id, format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"].lower(): h["value"]
                       for h in meta.get("payload", {}).get("headers", [])}
            sender = headers.get("from", "")
            subject = headers.get("subject", "(no subject)")
            sender = sender.split("<")[0].strip().strip('"') or sender
            return f'"{subject}"' + (f" from {sender}" if sender else "")
        except Exception:
            # Never block the action on a failed lookup; fall back to the id.
            return f"the email with id {message_id}"

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
            # One HTTP batch rather than a round-trip per hit. Twenty sequential
            # HTTPS calls put seconds of dead air into every "what's in my inbox".
            fetched: dict[str, dict] = {}

            def _collect(request_id, response, exception):
                if exception is None:
                    fetched[request_id] = response

            batch = _gmail().new_batch_http_request(callback=_collect)
            for i, stub in enumerate(stubs):
                batch.add(
                    _gmail().users().messages().get(
                        userId="me", id=stub["id"], format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    ),
                    request_id=str(i),
                )
            batch.execute()

            for i, stub in enumerate(stubs):
                msg = fetched.get(str(i))
                if msg is None:
                    lines.append(f"- id: {stub['id']} | (details unavailable)")
                    continue
                headers = msg.get("payload", {}).get("headers", [])
                lines.append(
                    f"- id: {stub['id']} | from: {_header(headers, 'From')} | "
                    f"subject: {_header(headers, 'Subject')} | "
                    f"date: {_header(headers, 'Date')} | snippet: {msg.get('snippet', '')}"
                )
            ctx.audit.record("tool_call", tool="search_email", detail=query)
            listing = "\n".join(lines)
            # Without this a capped search reads to the model as the complete
            # set, and Jarvis reports "you have 20" when there are hundreds.
            estimate = resp.get("resultSizeEstimate")
            if resp.get("nextPageToken") or (estimate and estimate > len(stubs)):
                listing += (
                    f"\n[Showing {len(stubs)} of about {estimate or 'many'} matches — "
                    "not the full set. Narrow the query, and say so when reporting "
                    "a count.]"
                )
            return as_document(f"gmail-search:{query}", listing)
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
            full = _extract_text(msg.get("payload", {}))
            body = full[:_MAX_BODY_CHARS]
            if len(full) > _MAX_BODY_CHARS:
                body += (
                    "\n[Truncated — the message is longer than the read limit, "
                    "so do not treat this as the whole email.]"
                )
            attached = _attachments(msg.get("payload", {}))
            attach_line = ""
            if attached:
                attach_line = (
                    "Attachments: " + ", ".join(attached)
                    + "\n[Jarvis can see that these exist but cannot open them from "
                      "here — say so rather than guessing at their contents.]\n"
                )
            text = (
                f"From: {_header(headers, 'From')}\n"
                f"To: {_header(headers, 'To')}\n"
                f"Subject: {_header(headers, 'Subject')}\n"
                f"Date: {_header(headers, 'Date')}\n"
                f"{attach_line}\n{body}"
            )
            ctx.audit.record("tool_call", tool="read_email", detail=message_id)
            return as_document(f"gmail:{message_id}", text)
        except Exception as e:
            ctx.audit.record("tool_call", tool="read_email", detail=message_id, ok=False)
            return f"Reading the email failed: {e}"

    @beta_tool
    def send_email(to: str, subject: str, body: str, cc: str = "",
                   attach_path: str = "", reply_to_message_id: str = "") -> str:
        """Send an email from the user's Gmail account, optionally attaching a
        local file. The user hears the recipient, subject, and any attachment
        and must explicitly confirm before anything is sent.

        Args:
            to: Recipient email address (comma-separate multiple addresses).
            subject: Email subject line.
            body: Plain-text body of the email.
            cc: Optional CC addresses, comma-separated.
            attach_path: Optional path of a local file (within the allowed
                folders configured for this machine) to attach.
            reply_to_message_id: When replying, the id of the message being
                replied to (from search_email). Keeps the reply in the same
                conversation instead of starting a new one.
        """
        if not ctx.permissions.require("email_send", "send email from your Gmail account"):
            return "The user declined email access."

        attachment = None
        if attach_path:
            from .local_files import PathNotAllowed, resolve_safe

            try:
                attachment = resolve_safe(ctx, attach_path)
            except PathNotAllowed as e:
                ctx.audit.record("tool_call", tool="send_email", detail=attach_path,
                                 decision="blocked_path", ok=False)
                return f"Blocked: {e}"
            if not attachment.is_file():
                return f"'{attachment}' does not exist or is not a file."
            if attachment.stat().st_size > 20_000_000:
                return (f"'{attachment.name}' is "
                        f"{attachment.stat().st_size // 1_000_000} MB — too large to "
                        "email (Gmail caps attachments around 25 MB). Try sharing it "
                        "via Drive instead.")

        preview = body if len(body) <= 200 else body[:200] + "…"
        summary = (
            f"I will send an email to {to}"
            + (f" (cc {cc})" if cc else "")
            + f' with the subject "{subject}"'
            + (f", attaching {attachment.name} "
               f"({max(1, attachment.stat().st_size // 1024)} KB)" if attachment else "")
            + f'. It begins: "{preview}".'
        )
        result = ctx.confirmer.confirm(
            "send_email", summary,
            audit_detail=(f'to={to} cc={cc} subject="{subject}"'
                          + (f" attach={attachment.name}" if attachment else "")),
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

            # Threading. Without In-Reply-To/References every "reply to
            # Ranjana" started a brand-new conversation, so the recipient lost
            # the context and the thread fragmented in both mailboxes.
            send_body: dict = {}
            if reply_to_message_id:
                try:
                    original = _gmail().users().messages().get(
                        userId="me", id=reply_to_message_id, format="metadata",
                        metadataHeaders=["Message-ID", "References", "Subject"],
                    ).execute()
                    orig_headers = original.get("payload", {}).get("headers", [])
                    parent = _header(orig_headers, "Message-ID")
                    if parent:
                        msg["In-Reply-To"] = parent
                        refs = _header(orig_headers, "References")
                        msg["References"] = f"{refs} {parent}".strip()
                    send_body["threadId"] = original.get("threadId")
                except Exception:
                    pass  # a failed lookup must not block the send
            if attachment:
                import mimetypes

                ctype, _ = mimetypes.guess_type(attachment.name)
                maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
                msg.add_attachment(attachment.read_bytes(), maintype=maintype,
                                   subtype=subtype, filename=attachment.name)
            encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            send_body["raw"] = encoded
            _gmail().users().messages().send(userId="me", body=send_body).execute()
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
                # Dead-ending here left the model with nowhere to go. Naming
                # the real labels lets it offer the closest one instead.
                available = ", ".join(sorted(label_cache)[:25]) or "(none found)"
                return (f"No label named '{label}' exists in this account. "
                        f"The labels that do exist are: {available}. Ask the user "
                        "which one they meant — Jarvis cannot create labels.")
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

        # Read back what the email actually IS. "(id 197f3a2b9c8d1e4f)" is
        # unverifiable by ear, so the user was consenting to trash something
        # they could not identify — a confirmation nobody can check is not a
        # confirmation.
        result = ctx.confirmer.confirm(
            "organize_email", f"I will {summary}: {_describe_message(message_id)}.",
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
