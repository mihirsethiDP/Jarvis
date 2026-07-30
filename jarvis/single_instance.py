"""One Jarvis per Windows user, enforced with a held file lock.

Probing the status port is not enough: the first launch spends minutes
loading speech models before it binds anything, so a second double-click in
that window sees a free port and starts a rival assistant. Two of them then
share one microphone and, worse, race on the same token and audit files —
a half-written Google token reads back as "not authorized", which looks
exactly like a broken sign-in.

The lock is an exclusive byte held on a file for the life of the process.
Windows drops it automatically when the process exits, including on a crash,
so a stale lock cannot strand the user.
"""

from __future__ import annotations

from pathlib import Path

from .paths import app_data_dir

try:
    import msvcrt
except ImportError:  # non-Windows (tests/CI elsewhere)
    msvcrt = None

_LOCK_NAME = "jarvis.lock"
_handle = None  # module-level so the lock outlives the acquiring call


def lock_path() -> Path:
    return app_data_dir() / _LOCK_NAME


def acquire() -> bool:
    """True if this process now owns the single-instance lock.

    The handle is deliberately never closed — releasing it would let a second
    instance in while this one is still running.
    """
    global _handle
    if _handle is not None:
        return True
    if msvcrt is None:
        return True  # nothing to enforce off Windows

    try:
        handle = open(lock_path(), "a+b")
    except OSError:
        # Can't create the lock file — better to run than to refuse to start.
        return True

    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False

    _handle = handle
    return True
