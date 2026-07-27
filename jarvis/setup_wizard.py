"""First-run consent wizard.

Run at install time (`jarvis setup`), this walks the employee through every
capability Jarvis could use and records an explicit decision for each:

- **allow**  — granted permanently (side effects still confirmed per action)
- **ask**    — Jarvis asks at the moment of first use (the default)
- **deny**   — standing denial: the tool is removed from the assistant's
  toolset entirely and Jarvis will not ask again. Re-run `jarvis setup` or
  `jarvis permissions revoke` to change your mind.

Decisions are per Windows user, stored in %APPDATA%\\Jarvis\\permissions.json.
"""

from __future__ import annotations

from .config import Config
from .io_channel import IOChannel, TextIO
from .paths import app_data_dir, cli_hint
from .security import AuditLog, PermissionManager

_MARKER = "setup_done"

# "yes"/"no" are deliberately absent: the prompt offers allow/ask/deny, and a
# bare "yes" should not silently become a permanent grant.
_ANSWERS = {
    "allow": "always", "a": "always", "always": "always",
    "ask": "ask", "k": "ask", "": "ask", "later": "ask",
    "deny": "denied", "d": "denied", "never": "denied",
}


def capability_specs(config: Config) -> list[tuple[str, str, str]]:
    """(capability, title, description) for everything this install could do."""
    specs = [
        ("files_read", "Local files — read",
         "list, search and read documents in your allowed folders"),
        ("files_write", "Local files — write",
         "create or modify text files in your allowed folders"),
        ("drive_read", "Google Drive — read",
         "search and read documents in your Google Drive"),
        ("drive_write", "Google Drive — write",
         "create files in / upload files to your Google Drive"),
        ("email_read", "Gmail — read",
         "search and read your inbox"),
        ("email_send", "Gmail — send",
         "send email from your account (every send is read back and confirmed)"),
        ("email_organize", "Gmail — organize",
         "archive, label, or trash mail (every change is confirmed, incl. mark read/unread)"),
        ("chat_read", "Google Chat — read",
         "see your Chat spaces, DMs, and messages in them"),
        ("chat_send", "Google Chat — send",
         "send Chat messages as you (every message is confirmed)"),
        ("calendar_read", "Calendar — read",
         "view your events and check free/busy for you or colleagues"),
        ("calendar_write", "Calendar — write",
         "create, change, or delete events (every change is confirmed — attendees get emailed)"),
        ("directory_read", "Company directory — read",
         "look up a colleague's email or phone number by name"),
        ("memory_recall", "Memory — recall",
         "use things it remembered in earlier conversations"),
        ("memory_write", "Memory — remember",
         "remember new facts between conversations (each one is confirmed first)"),
    ]
    for t in config.ai_tools:
        specs.append((f"ai:{t.name}", f"External AI — {t.name}",
                      f"forward prompts to '{t.name}' (each prompt is confirmed)"))
    for it in config.internal_tools:
        specs.append((f"tool:{it.name}:read", f"{it.description} — read",
                      f"read context from {it.name} as you"))
        if it.has_write_actions:
            specs.append((f"tool:{it.name}:write", f"{it.description} — write",
                          f"take write actions on {it.name} as you (each one confirmed)"))
    return specs


def setup_marker_exists() -> bool:
    return (app_data_dir() / _MARKER).exists()


def run_setup(
    config: Config,
    io: IOChannel | None = None,
    pm: PermissionManager | None = None,
) -> dict[str, str]:
    """Interactive consent flow. Returns {capability: decision} for reporting."""
    io = io or TextIO()
    if pm is None:
        pm = PermissionManager(io, AuditLog(anchored=True),
                               session_grant_minutes=config.session_grant_minutes)
    audit = pm.audit

    print(
        "\n=== Jarvis setup — access consent ===\n"
        "For each capability, answer:\n"
        "  allow  - Jarvis may use it (side effects are still confirmed each time)\n"
        "  ask    - Jarvis asks you at first use   [default]\n"
        "  deny   - Jarvis will NOT get this tool and will not ask\n"
        "You can re-run `jarvis setup` anytime to change these.\n"
    )

    decisions: dict[str, str] = {}
    try:
        for capability, title, description in capability_specs(config):
            while True:
                raw = io.ask(
                    f"{title}: Jarvis may {description}. [allow/ask/deny]"
                ).lower().strip()
                scope = _ANSWERS.get(raw)
                if scope is not None:
                    break
                print("Please answer allow, ask, or deny.")
            pm.set_grant(capability, scope)
            decisions[capability] = scope
    except EOFError:
        # Input closed mid-wizard (piped input, closed console): don't mark
        # setup complete on answers the employee never gave.
        audit.record("setup", detail="aborted: input closed", decision="aborted", ok=False)
        print("\nSetup aborted — nothing was finalized. Re-run: " + cli_hint("setup"))
        return decisions

    (app_data_dir() / _MARKER).write_text("1", encoding="utf-8")
    audit.record("setup", detail=f"decisions={decisions}")

    print("\nSummary:")
    for cap, scope in decisions.items():
        print(f"  {cap}: {scope}")
    print(
        "\nNext steps:\n"
        f"  - Claude API key:        {cli_hint('secrets set anthropic')}\n"
        f"  - Google authorization:  {cli_hint('setup-google')}  "
        "(if any Drive/Gmail/Chat/Calendar/Directory access was allowed)\n"
        + "".join(
            f"  - Connect {it.name}:  {cli_hint(f'secrets set tool-{it.name}-token')}\n"
            for it in config.internal_tools
            if decisions.get(f"tool:{it.name}:read") != "denied"
            or decisions.get(f"tool:{it.name}:write", "denied") != "denied"
        )
    )
    return decisions
