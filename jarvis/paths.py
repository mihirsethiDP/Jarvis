"""Well-known filesystem locations for Jarvis state on the local machine."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def cli_hint(subcommand: str) -> str:
    """A copy-pasteable command for this install — `jarvis` is not on PATH.

    Under the pythonw autostart, sys.executable is the console-less
    interpreter; swap it for python.exe so interactive commands work.
    """
    exe = sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"
    return f'"{exe}" -m jarvis {subcommand}'


def app_data_dir() -> Path:
    """Per-user writable directory for Jarvis state (grants, audit log, tokens)."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".jarvis"
    path = root / "Jarvis" if base else root
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_config_file() -> Path:
    return app_data_dir() / "config.yaml"


def permissions_file() -> Path:
    return app_data_dir() / "permissions.json"


def audit_log_file() -> Path:
    return app_data_dir() / "audit.jsonl"


def google_token_file() -> Path:
    """Fallback token location when the OS keyring is unavailable."""
    return app_data_dir() / "google_token.json"
