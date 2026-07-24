"""Secret storage backed by the OS keyring (Windows Credential Locker).

API keys and OAuth tokens never live in plaintext config. On machines where
a keyring backend is unavailable, callers may fall back to a file inside the
user's profile — that fallback is explicit, never silent.
"""

from __future__ import annotations

_SERVICE = "jarvis-assistant"


def _keyring():
    import keyring  # imported lazily so tests can run without a backend

    return keyring


def get_secret(name: str) -> str | None:
    try:
        return _keyring().get_password(_SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    try:
        _keyring().set_password(_SERVICE, name, value)
        return True
    except Exception:
        return False


def delete_secret(name: str) -> bool:
    try:
        _keyring().delete_password(_SERVICE, name)
        return True
    except Exception:
        return False
