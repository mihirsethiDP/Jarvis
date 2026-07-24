"""Append-only, hash-chained audit log.

Every tool invocation, permission decision, and side-effect confirmation is
recorded as one JSON line. Each entry carries a SHA-256 over the previous
entry's hash plus its own content, so any edit or deletion in the middle of
the log breaks the chain and is detectable with `jarvis audit --verify`.

Two hardening properties beyond the basic chain:

- **Cross-process safety.** The assistant and CLI commands may append
  concurrently; appends take an OS-level file lock and re-read the actual
  tail hash, so the chain never forks.
- **Truncation anchoring.** The head hash and entry count are mirrored into
  the Windows keyring after each append (for the real log only). Deleting
  the newest entries — which a bare hash chain cannot detect — then fails
  verification against the anchor. A same-user attacker who also rewrites
  the keyring anchor can still win; forwarding entries to an IT-controlled
  sink is the roadmap answer to that.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..paths import audit_log_file
from . import secrets as secret_store

try:
    import msvcrt
except ImportError:  # non-Windows (tests/CI on other platforms)
    msvcrt = None

_GENESIS = "0" * 64
_TAIL_BYTES = 8192
# Windows file locks are mandatory: locking byte 0 would block our own tail
# re-read. Serialize writers on a sentinel byte far past any real data.
_LOCK_OFFSET = 0x7FFF0000


class AuditLog:
    def __init__(self, path: Path | None = None, *, anchored: bool = False):
        self.path = path or audit_log_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._anchored = anchored
        self._anchor_name = (
            "audit-anchor-" + hashlib.sha256(str(self.path).encode()).hexdigest()[:16]
        )
        self._warned_degraded = False
        try:
            self._user = getpass.getuser()
        except Exception:
            self._user = "unknown"

    # -- tail / anchor helpers ---------------------------------------------
    def _read_tail_hash(self) -> str:
        """Read the hash of the last entry directly from disk."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return _GENESIS
        if size == 0:
            return _GENESIS
        try:
            with open(self.path, "rb") as fh:
                fh.seek(max(0, size - _TAIL_BYTES))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return _GENESIS
        for line in reversed(tail.splitlines()):
            if line.strip():
                try:
                    return json.loads(line).get("hash", _GENESIS)
                except json.JSONDecodeError:
                    return _GENESIS
        return _GENESIS

    def _load_anchor(self) -> dict | None:
        raw = secret_store.get_secret(self._anchor_name)
        if not raw:
            return None
        try:
            anchor = json.loads(raw)
            return anchor if isinstance(anchor, dict) else None
        except json.JSONDecodeError:
            return None

    def _store_anchor(self, head: str, count: int) -> None:
        secret_store.set_secret(
            self._anchor_name, json.dumps({"head": head, "count": count})
        )

    def _count_entries(self) -> int:
        if not self.path.exists():
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    # -- API -----------------------------------------------------------------
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
            # Binary mode: text-mode handles only support opaque seek cookies,
            # and we need real byte offsets for the lock sentinel.
            with self._lock, open(self.path, "a+b") as fh:
                locked = False
                if msvcrt is not None:
                    try:
                        fh.seek(_LOCK_OFFSET)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        locked = True
                    except OSError:
                        pass  # another writer holds it unusually long — proceed
                try:
                    # Re-read the real tail so concurrent writers (assistant +
                    # CLI) extend one chain instead of forking it.
                    entry["prev_hash"] = self._read_tail_hash()
                    body = json.dumps(entry, ensure_ascii=False, sort_keys=True)
                    entry["hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                    fh.seek(0, 2)
                    fh.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
                    fh.flush()
                    if self._anchored:
                        anchor = self._load_anchor()
                        count = (anchor["count"] + 1) if anchor else self._count_entries()
                        self._store_anchor(entry["hash"], count)
                finally:
                    if locked:
                        fh.seek(_LOCK_OFFSET)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError as e:
            if not self._warned_degraded:
                self._warned_degraded = True
                print(f"Warning: audit log unwritable ({e}) — events are being dropped.",
                      file=sys.stderr)

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
        """Walk the whole log verifying the hash chain (and anchor, if any).

        Returns (intact, entries_checked). A broken link means the file was
        edited in place; an anchor mismatch means the newest entries were
        removed or the file was replaced wholesale.
        """
        anchor = self._load_anchor() if self._anchored else None

        if not self.path.exists():
            return (anchor is None or anchor.get("count", 0) == 0), 0

        prev = _GENESIS
        count = 0
        anchor_head_seen = False
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
            if anchor and expected == anchor.get("head"):
                anchor_head_seen = True

        if anchor:
            if count < anchor.get("count", 0):
                return False, count  # newest entries were removed
            if not anchor_head_seen and anchor.get("count", 0) > 0:
                return False, count  # anchored head is gone — file replaced
        return True, count
