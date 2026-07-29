"""Google Chat tools: list spaces/DMs, read messages, send a message.

Runs as the signed-in employee via the same per-user OAuth as Drive/Gmail —
narrow scopes (chat.spaces.readonly, chat.messages.readonly,
chat.messages.create) rather than the broad chat.spaces/chat.messages, since
this feature set never needs to manage space membership or settings.

Note: message history in a space includes text from other members, not just
the employee — it is wrapped as untrusted data before reaching the model,
same as Gmail bodies and Drive documents.
"""

from __future__ import annotations

from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user

_MAX_SPACES = 25
_MAX_MESSAGES = 25


def build_tools(ctx: ToolContext) -> list:
    sender_names: dict[str, str] = {}  # "users/123" -> "Priya Rao", cached

    def _chat():
        return ctx.google_service("chat", "v1")

    def _sender_label(sender: dict) -> str:
        """Chat returns sender ids with no displayName under user auth, so a
        transcript would read "unknown: ..." for everyone. Resolve ids to
        names via the directory (same numeric id works as a People resource),
        cached, and fall back to the raw id if that isn't available."""
        given = sender.get("displayName")
        if given:
            return given
        resource = sender.get("name") or ""
        if not resource:
            return "unknown"
        if resource in sender_names:
            return sender_names[resource]
        label = resource
        # Skip lookups the employee declined; the ids still render.
        if not ctx.permissions.denied("directory_read"):
            try:
                person = ctx.google_service("people", "v1").people().get(
                    resourceName=f"people/{resource.split('/')[-1]}",
                    personFields="names",
                ).execute()
                label = (person.get("names") or [{}])[0].get("displayName") or resource
            except Exception:
                pass  # directory unavailable — the id is still informative
        sender_names[resource] = label
        return label

    @beta_tool
    def list_chat_spaces(name_filter: str = "") -> str:
        """List the user's Google Chat spaces and direct messages.

        Args:
            name_filter: Optional substring to filter space display names by.
        """
        if not ctx.permissions.require("chat_read", "see your Google Chat spaces and messages"):
            return "The user declined Google Chat access."
        try:
            resp = _chat().spaces().list(
                pageSize=_MAX_SPACES,
                filter='space_type = "SPACE" OR space_type = "DIRECT_MESSAGE" OR '
                       'space_type = "GROUP_CHAT"',
            ).execute()
            spaces = resp.get("spaces", [])
            if name_filter:
                needle = name_filter.lower()
                spaces = [s for s in spaces if needle in (s.get("displayName") or "").lower()]
            ctx.audit.record("tool_call", tool="list_chat_spaces", detail=name_filter)
            if not spaces:
                return "No matching Chat spaces or DMs found."
            lines = [
                f"- {s.get('displayName') or '(direct message)'}  (id: {s['name']}, "
                f"type: {s.get('spaceType', '?')})"
                for s in spaces
            ]
            return as_document("chat-spaces", "\n".join(lines))
        except Exception as e:
            ctx.audit.record("tool_call", tool="list_chat_spaces", ok=False)
            return f"Listing Chat spaces failed: {e}"

    @beta_tool
    def read_chat_messages(space_id: str, max_results: int = 15) -> str:
        """Read recent messages in a Chat space or DM (use list_chat_spaces
        first to find the space id, e.g. "spaces/AAAAxxxx").

        Args:
            space_id: The space resource name, e.g. "spaces/AAAAxxxx".
            max_results: Maximum number of recent messages to return (1-25).
        """
        if not ctx.permissions.require("chat_read", "see your Google Chat spaces and messages"):
            return "The user declined Google Chat access."
        try:
            resp = _chat().spaces().messages().list(
                parent=space_id,
                pageSize=max(1, min(int(max_results), _MAX_MESSAGES)),
            ).execute()
            messages = resp.get("messages", [])
            ctx.audit.record("tool_call", tool="read_chat_messages", detail=space_id)
            if not messages:
                return f"No messages found in {space_id}."
            lines = [
                f"[{m.get('createTime', '?')}] "
                f"{_sender_label(m.get('sender', {}))}: {m.get('text', '')}"
                for m in messages
            ]
            return as_document(f"chat:{space_id}", "\n".join(lines))
        except Exception as e:
            ctx.audit.record("tool_call", tool="read_chat_messages", detail=space_id, ok=False)
            return f"Reading Chat messages failed: {e}"

    @beta_tool
    def send_chat_message(space_id: str, text: str) -> str:
        """Send a plain-text Chat message as the user into a space or DM. The
        user hears the destination and text and must confirm before it sends.

        Args:
            space_id: The space resource name, e.g. "spaces/AAAAxxxx".
            text: Plain-text message to send (no rich cards under user auth).
        """
        if not ctx.permissions.require("chat_send", "send Google Chat messages as you"):
            return "The user declined Google Chat send access."

        preview = text if len(text) <= 200 else text[:200] + "…"
        result = ctx.confirmer.confirm(
            "send_chat_message",
            f'I will send this to {space_id}: "{preview}".',
            audit_detail=f"space={space_id} chars={len(text)}",
        )
        if not result:
            return cancelled_by_user(result, "sending that Chat message")

        try:
            sent = _chat().spaces().messages().create(
                parent=space_id, body={"text": text}
            ).execute()
            ctx.audit.record("tool_call", tool="send_chat_message",
                             detail=space_id, decision="confirmed")
            return f"Message sent to {space_id}: {sent.get('name', '')}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="send_chat_message", detail=space_id, ok=False)
            # Observed verbatim from Google on a real unconfigured project:
            # 404 "Google Chat app not found. To create a Chat app, you must
            # turn on the Chat API and configure the app in the Google Cloud
            # console." Reading Chat works without that step; sending doesn't,
            # and the raw error doesn't make the read/write split obvious.
            err_text = str(e)
            hint = ""
            if ("Chat app not found" in err_text or "configure the app" in err_text
                    or "403" in err_text or "PERMISSION_DENIED" in err_text.upper()):
                hint = (
                    " This usually means the Google Chat API still needs its "
                    "one-time app configuration: Cloud Console, APIs & Services, "
                    "Google Chat API, Configuration tab. Reading Chat works "
                    "without it; sending does not."
                )
            return f"Sending the Chat message failed: {e}.{hint}"

    return [list_chat_spaces, read_chat_messages, send_chat_message]
