"""Security posture check — `jarvis security-check`.

A deployed assistant drifts: someone grants a powerful capability "always",
a key ends up in a text file, a token falls back to plaintext, an audit log
gets edited. This walks the actual state of *this* machine and reports what
is safe, what is worth tightening, and what is an active risk.

It reads only local state and never sends anything anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..paths import app_data_dir, google_token_file
from .audit import AuditLog
from .permissions import PermissionManager

# Scopes that would hand Jarvis far more than it needs.
_OVERBROAD_SCOPES = {
    "https://www.googleapis.com/auth/drive": "full Drive read/write/delete",
    "https://mail.google.com/": "full mailbox access, including permanent delete",
    "https://www.googleapis.com/auth/gmail.settings.basic": "Gmail settings (filters, forwarding)",
    "https://www.googleapis.com/auth/gmail.settings.sharing": "Gmail forwarding and delegation",
    "https://www.googleapis.com/auth/chat.messages": "broad Chat read/write",
    "https://www.googleapis.com/auth/calendar": "full Calendar including sharing",
}

# Capabilities that deserve a second look when granted permanently.
_POWERFUL = {
    "code_run": "runs generated code on this machine",
    "email_send": "sends mail as the employee",
    "email_organize": "moves and trashes mail",
    "chat_send": "posts messages as the employee",
    "calendar_write": "creates and cancels meetings, emailing attendees",
    "files_write": "writes local files",
    "drive_write": "writes to Drive",
    "ask_claude": "sends prompts off-machine",
}

_SYNC_FOLDERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")


@dataclass
class Finding:
    level: str   # "ok" | "warn" | "risk"
    title: str
    detail: str = ""
    fix: str = ""


def _scan_for_plaintext_keys() -> list[Path]:
    """Look for an API key sitting in a readable file. Bounded to a few
    obvious places — this is a hygiene check, not a filesystem sweep."""
    home = Path.home()
    candidates: list[Path] = []
    roots = [home, home / "Desktop", home / "Downloads", home / "Documents",
             Path(__file__).resolve().parent.parent.parent]
    drive = Path(__file__).resolve().anchor
    if drive:
        roots.append(Path(drive))
    seen: set[Path] = set()
    for root in roots:
        try:
            if not root.is_dir() or root in seen:
                continue
            seen.add(root)
            for entry in root.iterdir():
                if not entry.is_file() or entry.suffix.lower() not in (".txt", ".env", ".md", ".json"):
                    continue
                if entry.stat().st_size > 200_000:
                    continue
                try:
                    if "sk-ant-" in entry.read_text(encoding="utf-8", errors="ignore"):
                        candidates.append(entry)
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return candidates


def run_checks(config: Config) -> list[Finding]:
    findings: list[Finding] = []
    add = findings.append

    # -- 1. secrets on disk ------------------------------------------------
    leaked = _scan_for_plaintext_keys()
    if leaked:
        add(Finding(
            "risk", f"API key found in {len(leaked)} plaintext file(s)",
            ", ".join(str(p) for p in leaked[:4]),
            "Delete the file(s). The key belongs only in Credential Manager "
            "(`jarvis secrets set anthropic`). Rotate the key if it was ever "
            "shared, pasted, or synced.",
        ))
    else:
        add(Finding("ok", "No API key found in plaintext files"))

    if os.environ.get("ANTHROPIC_API_KEY"):
        add(Finding(
            "warn", "API key is set as an environment variable",
            "Environment variables are visible to every process this user runs.",
            "Prefer Credential Manager only: unset the variable and use "
            "`jarvis secrets set anthropic`.",
        ))

    # -- 2. Google token storage ------------------------------------------
    dpapi_token = app_data_dir() / "google_token.bin"
    plain_token = google_token_file()
    if plain_token.exists():
        add(Finding(
            "risk", "Google token stored unencrypted",
            str(plain_token),
            "Install pywin32 so DPAPI encryption is available, delete the "
            "plaintext token, and re-run `jarvis setup-google`.",
        ))
    elif dpapi_token.exists():
        add(Finding("ok", "Google token is DPAPI-encrypted for this Windows user"))

    # -- 3. OAuth client file placement -----------------------------------
    secret = config.google_credentials_file
    if secret:
        path = Path(secret).expanduser()
        lowered = str(path).lower()
        if any(folder in lowered for folder in _SYNC_FOLDERS):
            add(Finding(
                "warn", "OAuth client file sits in a cloud-synced folder",
                str(path),
                "Move it somewhere local-only; synced copies spread the client "
                "secret to other devices and the vendor's servers.",
            ))
        elif path.exists():
            add(Finding("ok", "OAuth client file is in a local, non-synced location"))

    # -- 4. Google scopes --------------------------------------------------
    overbroad = [(s, why) for s, why in _OVERBROAD_SCOPES.items()
                 if s in config.google_scopes]
    if overbroad:
        add(Finding(
            "risk", f"{len(overbroad)} overbroad Google scope(s) requested",
            "; ".join(f"{s.rsplit('/', 1)[-1]} — {why}" for s, why in overbroad),
            "Narrow google.scopes in config, then have each employee re-run "
            "`jarvis setup-google`. Jarvis's defaults deliberately exclude "
            "full Drive, full mailbox access, and Gmail settings.",
        ))
    else:
        add(Finding("ok", f"All {len(config.google_scopes)} Google scopes are least-privilege",
                    "No full-Drive, full-mailbox, or Gmail-settings access requested."))

    # -- 5. standing grants ------------------------------------------------
    grants = PermissionManager(_SilentIO(), AuditLog()).list_grants()
    always = {c for c, m in grants.items() if m.get("scope") == "always"}
    denied = {c for c, m in grants.items() if m.get("scope") == "denied"}

    if "code_run" in always:
        add(Finding(
            "risk", "Code execution is permanently allowed",
            "code_run is granted 'always' — the most powerful capability Jarvis has.",
            "Unless this employee genuinely needs it, revoke: "
            "`jarvis permissions revoke code_run`, or deny it in `jarvis setup`.",
        ))
    elif "code_run" in denied:
        add(Finding("ok", "Code execution is denied outright",
                    "The run_code tool isn't even offered to the model."))

    risky_always = sorted(always & set(_POWERFUL) - {"code_run"})
    if risky_always:
        add(Finding(
            "warn", f"{len(risky_always)} powerful capability(ies) granted 'always'",
            "; ".join(f"{c} ({_POWERFUL[c]})" for c in risky_always),
            "'Always' skips the permission prompt — each action is still "
            "confirmed aloud, so this is a convenience/risk trade-off. Review "
            "with `jarvis permissions list`.",
        ))
    if denied:
        add(Finding("ok", f"{len(denied)} capability(ies) explicitly denied",
                    ", ".join(sorted(denied))))

    # -- 6. audit log integrity -------------------------------------------
    log = AuditLog(anchored=True)
    intact, count = log.verify_chain()
    if intact:
        add(Finding("ok", f"Audit chain intact ({count} entries verified)"))
    else:
        add(Finding(
            "risk", "Audit log fails verification",
            f"The hash chain breaks after {count} entries — the log was edited, "
            "truncated, or replaced.",
            "Treat as a possible tampering event: preserve the file, review "
            "recent activity, and report it.",
        ))

    # -- 7. spend brake ----------------------------------------------------
    limit = int(config.get("brain.daily_turn_limit", 0) or 0)
    if limit <= 0:
        add(Finding(
            "warn", "No daily usage limit",
            "brain.daily_turn_limit is 0 (unlimited).",
            "Set a limit so a loop or misuse can't run up the API bill, and "
            "add a spend cap on the key's workspace in the Anthropic Console.",
        ))
    else:
        add(Finding("ok", f"Daily usage limit set ({limit} turns/day)"))

    # -- 8. local UI exposure ---------------------------------------------
    if config.get("ui.enabled", False):
        add(Finding("ok", "Activity view enabled, bound to 127.0.0.1 only",
                    "The bind address is hard-coded; config cannot expose it to the LAN."))

    # -- 9. file allowlist breadth ----------------------------------------
    jarvis_root = Path(__file__).resolve().parent.parent.parent
    too_broad = []
    for directory in config.allowed_dirs:
        if str(directory) == directory.anchor:
            too_broad.append(f"{directory} (whole drive)")
        elif directory == Path.home():
            too_broad.append(f"{directory} (entire user profile)")
        else:
            try:
                if jarvis_root.is_relative_to(directory):
                    too_broad.append(f"{directory} (contains Jarvis's own code)")
            except ValueError:
                pass
    if too_broad:
        add(Finding(
            "risk", "File allowlist is too broad",
            "; ".join(too_broad),
            "List specific folders (Documents, Downloads, Desktop) instead. A "
            "drive root or the profile root exposes credentials and app data.",
        ))
    else:
        add(Finding("ok", f"File access limited to {len(config.allowed_dirs)} specific folder(s)"))

    return findings


class _SilentIO:
    """Read-only stand-in: the checkup must never prompt anybody."""

    def say(self, text: str) -> None:  # pragma: no cover - never used
        pass

    def ask(self, prompt: str) -> str:  # pragma: no cover - never used
        raise EOFError
