"""Only one Jarvis per Windows user.

Two instances were observed in the wild after a double-click on the desktop
shortcut: the first spends minutes loading speech models before it binds the
status port, so the old port-probe check saw a free port and let a second
assistant start. They then shared a microphone and raced on the Google token
file, and a torn read of that token surfaced as "Google isn't connected".
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from jarvis import single_instance


def test_second_process_is_refused_while_the_first_holds_the_lock(tmp_path, monkeypatch):
    if single_instance.msvcrt is None:
        pytest.skip("file locking is Windows-only")

    monkeypatch.setattr(single_instance, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(single_instance, "_handle", None)
    assert single_instance.acquire() is True

    # A real second process — a thread would share the same file handle and
    # would not reproduce the case this guards.
    probe = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(tmp_path.parent.parent)!r})
        from jarvis import single_instance
        single_instance.app_data_dir = lambda: __import__("pathlib").Path({str(tmp_path)!r})
        print(single_instance.acquire())
    """)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr


def test_acquire_is_idempotent_within_one_process(tmp_path, monkeypatch):
    monkeypatch.setattr(single_instance, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(single_instance, "_handle", None)
    assert single_instance.acquire() is True
    assert single_instance.acquire() is True   # same process may ask twice
