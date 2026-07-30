"""Open the orb as a desktop app window, with no terminal involved.

Running Jarvis from a console is fine for developers and hostile to everyone
else. Launched from the shortcut, `pythonw.exe -m jarvis --open-ui` starts the
assistant with no console at all and opens the status page in a chromeless
browser window, which is the closest thing to a native app that needs no
extra dependency and no code signing.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import webbrowser

# Edge ships on every Windows 11 machine; Chrome is the common alternative.
# `--app=` gives a window with no address bar, tabs, or browser chrome.
_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def is_running(port: int, host: str = "127.0.0.1") -> bool:
    """True if something already answers on the status port.

    Doubles as the single-instance check: two Jarvises sharing one microphone
    and one wake word would both try to answer, so the second launch should
    surface the first one's window instead of starting over.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def wait_until_up(port: int, timeout: float = 90.0) -> bool:
    """Wait for the status server. First run downloads speech models, which
    is slow enough that opening the window immediately shows an error page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(port):
            return True
        time.sleep(0.3)
    return False


def open_window(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    profile = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Jarvis", "window"
    )
    for exe in _BROWSERS:
        if os.path.exists(exe):
            try:
                # A dedicated profile dir keeps the app window out of the
                # employee's normal browsing session and stops it reopening
                # their tabs.
                subprocess.Popen(
                    [exe, f"--app={url}", f"--user-data-dir={profile}",
                     "--window-size=1100,720", "--no-first-run"],
                    close_fds=True,
                )
                return
            except OSError:
                continue
    fallback = shutil.which("msedge") or shutil.which("chrome")
    if fallback:
        try:
            subprocess.Popen([fallback, f"--app={url}"], close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url)   # ordinary tab — less pretty, always works
