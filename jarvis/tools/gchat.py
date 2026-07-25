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

from . import ToolContext, as_document

_MAX_SPACES = 25
_MAX_MESSAGES = 25


def build_tools(ctx: ToolContext) -> list:
    def _chat():
        return ctx.google_service("chat", "v1")

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
                f"{m.get('sender', {}).get('displayName', 'unknown')}: {m.get('text', '')}"
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
        if not ctx.confirmer.confirm(
            "send_chat_message",
            f'I will send this to {space_id}: "{preview}".',
            audit_detail=f"space={space_id} chars={len(text)}",
        ):
            return "Cancelled — the user did not confirm sending that Chat message."

        try:
            sent = _chat().spaces().messages().create(
                parent=space_id, body={"text": text}
            ).execute()
            ctx.audit.record("tool_call", tool="send_chat_message",
                             detail=space_id, decision="confirmed")
            return f"Message sent to {space_id}: {sent.get('name', '')}"
        except Exception as e:
            ctx.audit.record("tool_call", tool="send_chat_message", detail=space_id, ok=False)
            return f"Sending the Chat message failed: {e}"

    return [list_chat_spaces, read_chat_messages, send_chat_message]
