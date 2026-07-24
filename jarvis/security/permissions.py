"""Per-capability permission grants.

Capabilities are coarse, human-meaningful actions ("drive_read",
"email_send", "ai:company-gpt"). The first time a tool needs one, the user
is asked out loud (or in the console) and can grant it once, for the
session, or always. "Always" grants persist in a local JSON file the user
can review and revoke; session grants expire.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..io_channel import IOChannel
from ..paths import permissions_file
from .audit import AuditLog

_ALLOW_ALWAYS = {"always", "always allow", "allow always"}
_ALLOW_SESSION = {"session", "this session", "allow for this session", "for this session"}
_ALLOW_ONCE = {"once", "yes", "allow", "ok", "okay", "sure", "allow once", "yeah", "yep"}
_DENY = {"no", "deny", "don't", "dont", "never", "cancel", "stop"}


def normalize_answer(raw: str) -> str:
    """Lowercase and strip punctuation — speech-to-text produces 'Allow once.'"""
    return re.sub(r"[^\w\s']", " ", raw.lower()).strip().replace("  ", " ")


class PermissionManager:
    def __init__(
        self,
        io: IOChannel,
        audit: AuditLog,
        *,
        store_path: Path | None = None,
        session_grant_minutes: int = 480,
    ):
        self.io = io
        self.audit = audit
        self.store_path = store_path or permissions_file()
        self.session_ttl = session_grant_minutes * 60
        self._session: dict[str, float] = {}  # capability -> expiry epoch
        self._persistent: dict[str, dict] = self._load()

    # -- store ------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(self._persistent, indent=2), encoding="utf-8"
        )

    # -- API ---------------------------------------------------------------
    def granted(self, capability: str) -> bool:
        if self._persistent.get(capability, {}).get("scope") == "always":
            return True
        expiry = self._session.get(capability)
        return expiry is not None and expiry > time.time()

    def require(self, capability: str, description: str) -> bool:
        """Return True if the capability is granted, asking the user if needed."""
        if self.granted(capability):
            return True

        answer = normalize_answer(self.io.ask(
            f"I need permission to {description}. "
            'Say "allow once", "allow for this session", "always allow", or "deny".'
        ))

        if answer in _ALLOW_ALWAYS:
            self._persistent[capability] = {"scope": "always", "granted_at": time.time()}
            self._save()
            decision = "granted_always"
            allowed = True
        elif answer in _ALLOW_SESSION:
            self._session[capability] = time.time() + self.session_ttl
            decision = "granted_session"
            allowed = True
        elif answer in _ALLOW_ONCE:
            decision = "granted_once"
            allowed = True
        else:
            # Unrecognized answers fail closed — including silence.
            decision = "denied"
            allowed = False

        self.audit.record(
            "permission", tool=capability, detail=description, decision=decision, ok=allowed
        )
        return allowed

    def revoke(self, capability: str) -> bool:
        """Remove a persistent grant (used by `jarvis permissions revoke`)."""
        removed = self._persistent.pop(capability, None) is not None
        self._session.pop(capability, None)
        if removed:
            self._save()
        self.audit.record("permission", tool=capability, decision="revoked", ok=True)
        return removed

    def list_grants(self) -> dict[str, dict]:
        return dict(self._persistent)
