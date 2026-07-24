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
from .permissions import normalize_answer

_YES = {"yes", "confirm", "confirmed", "do it", "go ahead", "send it", "yep", "yeah", "ok", "okay"}


class Confirmer:
    def __init__(self, io: IOChannel, audit: AuditLog, *, enabled: bool = True):
        self.io = io
        self.audit = audit
        self.enabled = enabled

    def confirm(self, action: str, summary: str, *, audit_detail: str | None = None) -> bool:
        """Read the action back to the user; only an explicit yes proceeds.

        *summary* is spoken to the user and should be concrete (it may quote
        content); *audit_detail* is what lands in the audit log — pass a
        content-free variant when the summary contains message bodies.
        """
        detail = audit_detail if audit_detail is not None else summary
        if not self.enabled:
            self.audit.record("confirmation", tool=action, detail=detail,
                              decision="skipped_disabled", ok=True)
            return True

        answer = normalize_answer(self.io.ask(
            f"Please confirm — {summary} Say yes to proceed, or no to cancel."
        ))
        confirmed = answer in _YES
        self.audit.record(
            "confirmation",
            tool=action,
            detail=detail,
            decision="confirmed" if confirmed else "cancelled",
            ok=confirmed,
        )
        return confirmed
