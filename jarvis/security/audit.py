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
import os
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
        # Live observers (the activity feed). Every action already funnels
        # through record(), so subscribing here gives complete visibility
        # without instrumenting each tool separately.
        self._subscribers: list = []
        try:
            self._user = getpass.getuser()
        except Exception:
            self._user = "unknown"

    def subscribe(self, callback) -> None:
        """Call *callback(entry_dict)* on every recorded event. Never let a
        subscriber's failure break auditing or the assistant."""
        self._subscribers.append(callback)

    def _notify(self, entry: dict) -> None:
        for callback in list(self._subscribers):
            try:
                callback(dict(entry))
            except Exception:
                pass

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
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass  # best effort; the read-back below is the real check
                    # Prove the bytes landed before touching the anchor. A
                    # write that is accepted and then discarded (antivirus,
                    # folder sync, a mandatory lock held elsewhere) would
                    # otherwise leave the anchor counting events the log never
                    # received — and every later verification would report
                    # "tampering" that no one can distinguish from the real
                    # thing. Failing loudly here is the whole point: an audit
                    # log dropping events is itself a security event.
                    if self._read_tail_hash() != entry["hash"]:
                        raise OSError(
                            "the entry was written but is not in the file afterwards"
                        )
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
                print(f"SECURITY WARNING: the audit log is not recording ({e}). "
                      "Jarvis is still running but its activity is NOT being logged. "
                      "Check antivirus or folder-sync interference on the log file.",
                      file=sys.stderr)
        self._notify(entry)

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

        Returns (intact, entries_checked). See `verify` for why the failure
        actually happened.
        """
        intact, count, _, _ = self.verify()
        return intact, count

    def verify(self) -> tuple[bool, int, str, int]:
        """Verify the chain and report *how* it failed.

        Returns (intact, entries_checked, reason, missing). The reason matters
        because the failures are not equally alarming:

        - ``entry_modified`` / ``chain_broken`` — an entry in the file was
          edited. The file itself is evidence of tampering.
        - ``entries_missing`` — every entry present is perfectly chained, but
          the anchor counted more than the file holds. That is either a
          truncated tail (tampering) *or* writes the filesystem accepted and
          discarded. Both are real; they need different responses, so callers
          must not report one as the other.

        ``missing`` is how many entries the anchor counted beyond the file.
        """
        anchor = self._load_anchor() if self._anchored else None

        if not self.path.exists():
            if anchor and anchor.get("count", 0) > 0:
                return False, 0, "entries_missing", anchor["count"]
            return True, 0, "", 0

        prev = _GENESIS
        count = 0
        anchor_head_seen = False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, count, "unreadable_entry", 0
            expected = entry.pop("hash", None)
            if entry.get("prev_hash") != prev:
                return False, count, "chain_broken", 0
            body = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != expected:
                return False, count, "entry_modified", 0
            prev = expected
            count += 1
            if anchor and expected == anchor.get("head"):
                anchor_head_seen = True

        if anchor:
            anchored_count = anchor.get("count", 0)
            if count < anchored_count:
                return False, count, "entries_missing", anchored_count - count
            if not anchor_head_seen and anchored_count > 0:
                # Head absent although the file is long enough: the tail was
                # replaced rather than merely shortened.
                return False, count, "anchor_head_absent", 0
        return True, count, "", 0

    def reanchor(self) -> tuple[bool, str]:
        """Re-point the anchor at the current file, after drift was reviewed.

        Deliberately narrow: this is refused unless every entry in the file
        chains correctly, so a *modified* log can never be papered over. The
        re-anchor is itself written to the log first, leaving the gap
        permanently visible instead of erasing the evidence.
        """
        intact, count, reason, missing = self.verify()
        if reason in ("chain_broken", "entry_modified", "unreadable_entry"):
            return False, (
                f"Refusing to re-anchor: the log's own chain is broken ({reason}). "
                "Entries were modified — preserve this file and report it."
            )
        was_anchored, self._anchored = self._anchored, False
        try:
            self.record("audit", tool="reanchor",
                        detail=f"anchor was ahead by {missing} entry(ies); "
                               f"re-anchored to {count + 1} on disk",
                        decision="cli", ok=False)
        finally:
            self._anchored = was_anchored
        head = self._read_tail_hash()
        self._store_anchor(head, self._count_entries())
        return True, (
            f"Re-anchored to the {self._count_entries()} entries on disk. "
            f"{missing} entry(ies) the anchor had counted are gone for good — "
            "that gap is now recorded in the log itself."
        )
