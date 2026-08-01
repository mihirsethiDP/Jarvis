"""Plain-English descriptions of what a tool call is doing.

Tool work is where a turn's seconds go. Without narration the user finishes
speaking and then sits in silence for ten or twenty seconds with no idea
whether Jarvis is working, stuck, or already done — which is exactly what
"it gives me stupid outcomes" feels like from the outside.

These strings are spoken aloud and shown on the status page, so they are
written the way a person would say them, in the present continuous, and they
name the *subject* where the arguments make it available ("Looking up Ranjana
in the company directory", not "Calling find_direct_message").
"""

from __future__ import annotations

# Argument to quote for each tool, in priority order. The first one present
# and non-empty is used as the subject of the phrase.
_SUBJECT_KEYS = (
    "person_name", "name", "query", "name_filter", "space_id", "to",
    "path", "file_path", "folder", "city", "location", "fact", "fact_id",
    "action", "prompt", "text", "subject",
)

_TEMPLATES = {
    # Google Chat
    "find_direct_message": "Looking up {subject} so I can find your chat with them",
    "list_chat_spaces": "Checking your chat spaces",
    "read_chat_messages": "Reading that chat",
    "send_chat_message": "Getting that chat message ready",
    # Gmail
    "search_email": "Searching your inbox",
    "read_email": "Opening that email",
    "send_email": "Getting that email ready",
    "organize_email": "Tidying that mail",
    # Drive
    "drive_search": "Searching your Drive",
    "drive_read": "Reading that document",
    "drive_save_text": "Saving that to Drive",
    "drive_upload": "Uploading that to Drive",
    # Calendar
    "list_calendar_events": "Checking your calendar",
    "check_availability": "Checking who is free",
    "create_calendar_event": "Setting up that meeting",
    "delete_calendar_event": "Cancelling that meeting",
    # Directory
    "find_colleague": "Looking up {subject} in the company directory",
    # Local files
    "list_folder": "Looking in that folder",
    "search_files": "Searching your files",
    "read_file": "Reading that file",
    "write_file": "Writing that file",
    # Knowledge
    "search_web": "Searching the web",
    "get_weather": "Checking the weather",
    "ask_claude": "Thinking that through",
    "run_code": "Working that out",
    # Memory
    "remember": "Making a note of that",
    "forget_fact": "Forgetting that",
}

_MAX_SUBJECT = 48

# Wording for when the subject is missing or unspeakable (a space id, a whole
# message body). Only needed for templates containing {subject}.
_FALLBACKS = {
    "find_direct_message": "Finding your chat with them",
    "find_colleague": "Checking the company directory",
}


def _subject(args: dict) -> str:
    for key in _SUBJECT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            # A space id or a wall of message text makes a terrible spoken
            # subject; keep it only when it reads like a name or short phrase.
            if text.startswith("spaces/") or len(text) > _MAX_SUBJECT:
                return ""
            return text
    return ""


def describe_tool(tool_name: str, args: dict | None = None) -> str:
    """One short spoken clause for a tool call in progress."""
    args = args or {}
    template = _TEMPLATES.get(tool_name)
    if template is None:
        # An internal tool declared in config, or something new. Say the name
        # in a readable way rather than staying silent about it.
        readable = tool_name.replace("_", " ").strip()
        return f"Working on {readable}" if readable else "Working on that"
    if "{subject}" not in template:
        return template
    subject = _subject(args)
    if not subject:
        # "Looking up  in the company directory" is worse than saying nothing
        # specific, so each subject template carries a subject-free wording.
        return _FALLBACKS.get(tool_name, "Working on that")
    return template.format(subject=subject)
