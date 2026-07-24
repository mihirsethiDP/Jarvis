"""Append-only, hash-chained audit log.

Every tool invocation, permission decision, and side-effect confirmation is
recorded as one JSON line. Each entry carries a SHA-256 over the previous
entry's hash plus its own content, so any edit or deletion in the middle of
the log breaks the chain and is detectable with `jarvis audit --verify`.
The log is local to the employee's machine and is the first place to look
when reviewing what the assistant actually did.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..paths import audit_log_file

_GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = path or audit_log_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev_hash = self._last_hash()
        try:
            self._user = getpass.getuser()
        except Exception:
            self._user = "unknown"

    def _last_hash(self) -> str:
        if not self.path.exists():
            return _GENESIS
        last = None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last = line
        except OSError:
            return _GENESIS
        if not last:
            return _GENESIS
        try:
            return json.loads(last).get("hash", _GENESIS)
        except json.JSONDecodeError:
            return _GENESIS

    def record(
        self,
        event: str,
        *,
        tool: str = "",
        detail: str = "",
        decision: str = "",
        ok: bool = True,
    ) -> None:
        """Append one event. Never raises — auditing must not break the assistant."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user": self._user,
            "event": event,        # e.g. tool_call | permission | confirmation | error
            "tool": tool,
            "detail": detail[:2000],
            "decision": decision,  # e.g. allowed | denied | confirmed | cancelled
            "ok": ok,
        }
        try:
            with self._lock:
                entry["prev_hash"] = self._prev_hash
                body = json.dumps(entry, ensure_ascii=False, sort_keys=True)
                entry["hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._prev_hash = entry["hash"]
        except OSError:
            pass

    def tail(self, n: int = 20) -> list[dict]:
        """Return the most recent *n* entries (for `jarvis audit`)."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def verify_chain(self) -> tuple[bool, int]:
        """Walk the whole log verifying the hash chain.

        Returns (intact, entries_checked). A broken link means the file was
        edited or truncated after the fact.
        """
        if not self.path.exists():
            return True, 0
        prev = _GENESIS
        count = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, count
            expected = entry.pop("hash", None)
            if entry.get("prev_hash") != prev:
                return False, count
            body = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != expected:
                return False, count
            prev = expected
            count += 1
        return True, count
