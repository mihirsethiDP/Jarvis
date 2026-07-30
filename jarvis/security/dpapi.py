"""DPAPI-encrypted file storage for blobs too large for the Windows keyring.

Windows Credential Manager caps blobs at 2560 bytes — Google OAuth token JSON
routinely exceeds that. Larger secrets are encrypted with the user's DPAPI
master key (via pywin32) and written under %APPDATA%\\Jarvis. Decryption only
works for the same Windows user on the same machine.
"""

from __future__ import annotations

import os
from pathlib import Path


def _win32crypt():
    import win32crypt  # pywin32; Windows only

    return win32crypt


def protect_to_file(path: Path, data: bytes, description: str = "jarvis") -> bool:
    """Encrypt *data* with DPAPI and write it to *path*. Returns False if
    DPAPI is unavailable (caller decides on a fallback)."""
    try:
        blob = _win32crypt().CryptProtectData(data, description, None, None, None, 0)
    except Exception:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace, not a truncate-then-write. A reader that catches the
    # gap gets an empty file, fails to decrypt it, and reports "not
    # authorized" — indistinguishable from a genuinely missing sign-in.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def unprotect_from_file(path: Path) -> bytes | None:
    """Read and decrypt a DPAPI blob. Returns None if the file is missing,
    DPAPI is unavailable, or the blob can't be decrypted (e.g. profile reset) —
    callers should treat None as 'not authorized yet' and re-run their flow."""
    if not path.exists():
        return None
    try:
        _desc, data = _win32crypt().CryptUnprotectData(
            path.read_bytes(), None, None, None, 0
        )
        return data
    except Exception:
        return None
