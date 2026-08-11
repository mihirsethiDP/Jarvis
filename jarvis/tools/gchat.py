"""Google Chat tools: find a person's DM, list spaces, read and send messages.

Runs as the signed-in employee via the same per-user OAuth as Drive/Gmail —
narrow scopes (chat.spaces.readonly, chat.messages.readonly,
chat.messages.create) rather than the broad chat.spaces/chat.messages, since
this feature set never needs to manage space membership or settings.

Two facts about the Chat API shape everything here, both confirmed against a
real account:

1. **Direct messages have no displayName.** All 30 of one employee's DMs come
   back with ``displayName: None``. Matching a person's name against space
   display names therefore never finds a DM — "message Ranjana" answered "no
   space exists" while her DM was sitting there. Person-to-DM resolution goes
   through ``spaces.findDirectMessage``, which takes a Chat user id, so the
   directory lookup has to hand back resource ids and not just email
   addresses.

2. **Space lists paginate.** That same account has 274 spaces across three
   pages. Reading only the first page silently hides most of them, so a space
   the employee names can be reported as non-existent.

Message history in a space includes text from other members, not just the
employee — it is wrapped as untrusted data before reaching the model, same as
Gmail bodies and Drive documents.
"""

from __future__ import annotations

from anthropic import beta_tool

from . import ToolContext, as_document, cancelled_by_user

_PAGE_SIZE = 100
_MAX_PAGES = 10          # 1000 spaces; far past any real account
_MAX_SHOWN = 25
_MAX_MESSAGES = 25
_MAX_LABELLED = 18       # DMs we name by reading a message (one call each)


def build_tools(ctx: ToolContext) -> list:
    people_names: dict[str, str] = {}   # "users/123" -> "Priya Rao"
    space_labels: dict[str, str] = {}   # "spaces/abc" -> "Ranjana Majumdar (DM)"
    me_cache: dict[str, str] = {}

    def _chat():
        return ctx.google_service("chat", "v1")

    def _people():
        return ctx.google_service("people", "v1")

    # -- identity helpers -------------------------------------------------
    def _me() -> str:
        """The signed-in user's own Chat id, so a DM can be labelled by the
        *other* participant rather than by whoever spoke last."""
        if "id" not in me_cache:
            me_cache["id"] = ""
            try:
                who = _people().people().get(
                    resourceName="people/me", personFields="names").execute()
                me_cache["id"] = "users/" + who["resourceName"].split("/")[-1]
            except Exception:
                pass
        return me_cache["id"]

    def _person_name(user_resource: str) -> str:
        """Resolve "users/123" to a display name via the directory, cached."""
        if not user_resource:
            return "unknown"
        if user_resource in people_names:
            return people_names[user_resource]
        label = user_resource
        if not ctx.permissions.denied("directory_read"):
            try:
                person = _people().people().get(
                    resourceName=f"people/{user_resource.split('/')[-1]}",
                    personFields="names",
                ).execute()
                label = (person.get("names") or [{}])[0].get("displayName") or user_resource
            except Exception:
                pass  # directory unavailable — the id is still informative
        people_names[user_resource] = label
        return label

    def _sender_label(sender: dict) -> str:
        return sender.get("displayName") or _person_name(sender.get("name") or "")

    def _search_directory(name: str) -> list[dict]:
        resp = _people().people().searchDirectoryPeople(
            query=name,
            readMask="names,emailAddresses",
            sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            pageSize=20,
        ).execute()
        return resp.get("people", [])

    def _describe(person: dict) -> str:
        display = (person.get("names") or [{}])[0].get("displayName", "?")
        email = (person.get("emailAddresses") or [{}])[0].get("value", "")
        return f"{display} <{email}>" if email else display

    # -- space helpers ----------------------------------------------------
    def _all_spaces() -> list[dict]:
        """Every space, following pagination. Reading only page one hides
        most of a real account's spaces."""
        spaces, token, pages = [], None, 0
        while pages < _MAX_PAGES:
            resp = _chat().spaces().list(pageSize=_PAGE_SIZE, pageToken=token).execute()
            spaces.extend(resp.get("spaces", []))
            token = resp.get("nextPageToken")
            pages += 1
            if not token:
                break
        return spaces

    def _dm_partner(space_id: str) -> str:
        """Name the other person in a DM by looking at who has spoken in it.
        Membership listing needs a scope Jarvis deliberately does not hold, so
        the message history is the cheapest honest source."""
        try:
            resp = _chat().spaces().messages().list(parent=space_id, pageSize=8).execute()
        except Exception:
            return ""
        mine = _me()
        for m in resp.get("messages", []):
            sender = (m.get("sender") or {}).get("name") or ""
            if sender and sender != mine:
                return _person_name(sender)
        return ""

    def _space_label(space: dict) -> str:
        """A label a human can actually verify. Confirming a send against
        "spaces/0LhmHiAAAAE" tells the employee nothing about who is about to
        receive it, which makes the confirmation gate worthless."""
        space_id = space.get("name", "")
        if space_id in space_labels:
            return space_labels[space_id]
        display = space.get("displayName")
        kind = space.get("spaceType", "")
        if display:
            label = f"{display} ({'group' if kind == 'GROUP_CHAT' else 'space'})"
        elif kind == "DIRECT_MESSAGE":
            partner = _dm_partner(space_id)
            label = f"{partner} (direct message)" if partner else "a direct message"
        elif kind == "GROUP_CHAT":
            partner = _dm_partner(space_id)
            label = f"group chat with {partner}" if partner else "an unnamed group chat"
        else:
            label = "an unnamed space"
        space_labels[space_id] = label
        return label

    def _label_for_id(space_id: str) -> str:
        for space in _all_spaces():
            if space.get("name") == space_id:
                return _space_label(space)
        return space_id

    # -- tools ------------------------------------------------------------
    @beta_tool
    def find_direct_message(person_name: str) -> str:
        """Find the Google Chat direct-message space for a colleague, by name.

        Use this whenever the user wants to message a person (rather than a
        named group space). Direct messages have no display name, so they can
        never be found with list_chat_spaces — this is the only way.

        Args:
            person_name: The colleague's name, e.g. "Ranjana" or "Ranjana Majumdar".
        """
        if not ctx.permissions.require("chat_read", "see your Google Chat spaces and messages"):
            return "The user declined Google Chat access."
        if not ctx.permissions.require("directory_read",
                                       "look up colleagues in your company directory"):
            return ("Finding someone's DM needs company directory access, which "
                    "the user declined.")
        try:
            matches = _search_directory(person_name)
        except Exception as e:
            ctx.audit.record("tool_call", tool="find_direct_message",
                             detail=person_name, ok=False)
            return f"Directory lookup failed: {e}"

        if not matches:
            ctx.audit.record("tool_call", tool="find_direct_message",
                             detail=f"{person_name} -> no directory match", ok=False)
            return (f"No one called '{person_name}' is in the company directory. "
                    "Ask the user for the person's full name or email address.")
        if len(matches) > 1:
            # Never guess between colleagues — sending to the wrong person is
            # not recoverable once it lands.
            listing = "\n".join(f"- {_describe(p)}" for p in matches[:10])
            ctx.audit.record("tool_call", tool="find_direct_message",
                             detail=f"{person_name} -> {len(matches)} ambiguous")
            return (f"AMBIGUOUS: {len(matches)} people match '{person_name}'. Do NOT "
                    "choose one yourself — ask the user which they mean, then call "
                    f"this tool again with the full name.\n{listing}")

        person = matches[0]
        display = (person.get("names") or [{}])[0].get("displayName", person_name)
        user_id = "users/" + person["resourceName"].split("/")[-1]
        try:
            dm = _chat().spaces().findDirectMessage(name=user_id).execute()
        except Exception as e:
            ctx.audit.record("tool_call", tool="find_direct_message",
                             detail=f"{display}: no DM", ok=False)
            if "404" in str(e) or "NOT_FOUND" in str(e).upper():
                return (f"{display} is in the directory, but you have no existing "
                        "Chat direct message with them. Jarvis can only send into "
                        "conversations that already exist — tell the user they need "
                        "to open the DM in Google Chat once, or offer to send an "
                        f"email to {_describe(person)} instead.")
            return f"Could not look up the direct message with {display}: {e}"

        space_id = dm.get("name", "")
        space_labels[space_id] = f"{display} (direct message)"
        ctx.audit.record("tool_call", tool="find_direct_message",
                         detail=f"{display} -> {space_id}")
        return (f"Direct message with {display}: space id {space_id}. "
                f"Use send_chat_message with this space id to message them.")

    @beta_tool
    def list_chat_spaces(name_filter: str = "") -> str:
        """List the user's Google Chat spaces and group chats.

        To message a PERSON, use find_direct_message instead — direct messages
        have no display name and will not appear in a name search here.

        Args:
            name_filter: Optional substring to filter space names by.
        """
        if not ctx.permissions.require("chat_read", "see your Google Chat spaces and messages"):
            return "The user declined Google Chat access."
        try:
            spaces = _all_spaces()
        except Exception as e:
            ctx.audit.record("tool_call", tool="list_chat_spaces", ok=False)
            return f"Listing Chat spaces failed: {e}"

        named = [s for s in spaces if s.get("displayName")]
        unnamed = [s for s in spaces if not s.get("displayName")]

        if name_filter:
            needle = name_filter.lower()
            hits = [s for s in named if needle in (s.get("displayName") or "").lower()]
            # Fall back to naming the unnamed ones only when a plain match
            # failed, so the common case stays a single API call.
            if not hits:
                for space in unnamed[:_MAX_LABELLED]:
                    if needle in _space_label(space).lower():
                        hits.append(space)
            shown = hits
        else:
            shown = named + unnamed[:_MAX_LABELLED]

        ctx.audit.record("tool_call", tool="list_chat_spaces",
                         detail=f"{name_filter or '(all)'} -> {len(shown)}/{len(spaces)}")
        if not shown:
            hint = ""
            if name_filter:
                hint = (f" If '{name_filter}' is a person rather than a group, call "
                        "find_direct_message instead — DMs never appear by name here.")
            return f"No matching Chat spaces found among {len(spaces)} spaces." + hint

        lines = [f"- {_space_label(s)}  (id: {s['name']})" for s in shown[:_MAX_SHOWN]]
        body = "\n".join(lines)
        if len(shown) > _MAX_SHOWN:
            body += f"\n[{len(shown) - _MAX_SHOWN} more not shown — narrow the filter.]"
        return as_document("chat-spaces", body)

    @beta_tool
    def read_chat_messages(space_id: str, max_results: int = 15) -> str:
        """Read recent messages in a Chat space or DM.

        Args:
            space_id: The space resource name, e.g. "spaces/AAAAxxxx". Get it
                from find_direct_message (for a person) or list_chat_spaces.
            max_results: Maximum number of recent messages to return (1-25).
        """
        if not ctx.permissions.require("chat_read", "see your Google Chat spaces and messages"):
            return "The user declined Google Chat access."
        if not str(space_id).startswith("spaces/"):
            return ("space_id must look like 'spaces/AAAAxxxx'. Use "
                    "find_direct_message for a person, or list_chat_spaces for a group.")
        try:
            resp = _chat().spaces().messages().list(
                parent=space_id,
                pageSize=max(1, min(int(max_results), _MAX_MESSAGES)),
                # The API defaults to oldest-first, so "recent messages" was
                # returning the opening lines of a years-old conversation.
                orderBy="createTime desc",
            ).execute()
            # Fetched newest-first, shown oldest-first so the transcript reads
            # in the order it was said.
            messages = list(reversed(resp.get("messages", [])))
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
        user hears who it is going to and what it says, and must confirm.

        Args:
            space_id: The space resource name, e.g. "spaces/AAAAxxxx". Get it
                from find_direct_message (for a person) or list_chat_spaces.
            text: Plain-text message to send (no rich cards under user auth).
        """
        if not ctx.permissions.require("chat_send", "send Google Chat messages as you"):
            return "The user declined Google Chat send access."
        if not str(space_id).startswith("spaces/"):
            return ("space_id must look like 'spaces/AAAAxxxx'. To message a person, "
                    "call find_direct_message first to get their DM space id.")

        # Resolve to something the user can actually check. A confirmation
        # reading "send to spaces/0LhmHiAAAAE" cannot be verified by a human,
        # which defeats the point of asking.
        try:
            recipient = space_labels.get(space_id) or _label_for_id(space_id)
        except Exception:
            recipient = space_id

        preview = text if len(text) <= 200 else text[:200] + "…"
        result = ctx.confirmer.confirm(
            "send_chat_message",
            f'I will send this to {recipient}: "{preview}".',
            audit_detail=f"space={space_id} to={recipient} chars={len(text)}",
        )
        if not result:
            return cancelled_by_user(result, f"sending that Chat message to {recipient}")

        try:
            sent = _chat().spaces().messages().create(
                parent=space_id, body={"text": text}
            ).execute()
            ctx.audit.record("tool_call", tool="send_chat_message",
                             detail=f"{space_id} ({recipient})", decision="confirmed")
            return f"Message sent to {recipient}: {sent.get('name', '')}"
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

    return [find_direct_message, list_chat_spaces, read_chat_messages, send_chat_message]
