"""Explicit confirmation before side effects.

Permission answers *may this class of action happen at all*; confirmation
answers *should this specific action happen right now*. Anything that sends
data off the machine or modifies data (email, uploads, file writes) reads a
summary back to the user and requires an explicit yes — even if the
capability was granted "always".
"""

from __future__ import annotations

from ..io_channel import IOChannel
from .audit import AuditLog

_YES = {"yes", "confirm", "confirmed", "do it", "go ahead", "send it", "yep", "yeah", "ok", "okay"}


class Confirmer:
    def __init__(self, io: IOChannel, audit: AuditLog, *, enabled: bool = True):
        self.io = io
        self.audit = audit
        self.enabled = enabled

    def confirm(self, action: str, summary: str) -> bool:
        """Read the action back to the user; only an explicit yes proceeds."""
        if not self.enabled:
            self.audit.record("confirmation", tool=action, detail=summary,
                              decision="skipped_disabled", ok=True)
            return True

        answer = self.io.ask(
            f"Please confirm — {summary} Say yes to proceed, or no to cancel."
        ).lower().strip()
        confirmed = answer in _YES
        self.audit.record(
            "confirmation",
            tool=action,
            detail=summary,
            decision="confirmed" if confirmed else "cancelled",
            ok=confirmed,
        )
        return confirmed
