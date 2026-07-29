"""Blast-radius limits on side effects.

Every gate in Jarvis is designed to stop a bad action. This is the layer
that assumes one got through anyway — a convincing prompt injection, a
misread confirmation, a genuine mistake — and caps how much damage is
possible before a human notices.

Concretely: a compromised assistant should not be able to mail the whole
company. Each side-effecting action class gets a rolling hourly and daily
ceiling; hitting one refuses the action outright (the user is told, and it
is audited) rather than asking for confirmation again.

Counts are per Windows user in %APPDATA%\\Jarvis\\limits.json — one
employee's activity never affects another's.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from ..paths import app_data_dir

# (per hour, per day) per action. Deliberately generous for normal work and
# tight enough that a runaway loop or injection is caught quickly.
DEFAULT_CAPS: dict[str, tuple[int, int]] = {
    "send_email": (15, 60),
    "send_chat_message": (25, 100),
    "create_calendar_event": (15, 50),
    "delete_calendar_event": (10, 30),
    "organize_email": (60, 300),
    "drive_upload": (20, 80),
    "drive_save_text": (20, 80),
    "write_file": (30, 120),
    "run_code": (20, 60),
    "ask_ai_tool": (20, 80),
}
_FALLBACK_CAP = (40, 200)


def limits_file() -> Path:
    return app_data_dir() / "limits.json"


class ActionLimiter:
    def __init__(
        self,
        caps: dict[str, tuple[int, int]] | None = None,
        path: Path | None = None,
        now: Callable[[], float] | None = None,
    ):
        self.caps = dict(DEFAULT_CAPS if caps is None else caps)
        self.path = path or limits_file()
        self._now = now or time.time
        self._lock = threading.Lock()

    # -- storage -----------------------------------------------------------
    def _load(self) -> dict[str, list[float]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
        except (json.JSONDecodeError, OSError):
            return {}
        cutoff = self._now() - 86_400
        # Drop anything older than a day on read: the file stays small and a
        # tampered/garbage entry can't accumulate.
        return {
            str(action): [float(t) for t in stamps
                          if isinstance(t, (int, float)) and float(t) > cutoff]
            for action, stamps in data.items() if isinstance(stamps, list)
        }

    def _save(self, data: dict[str, list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    # -- API ---------------------------------------------------------------
    def check(self, action: str) -> str | None:
        """Return None if the action may proceed, else a reason to refuse."""
        hourly, daily = self.caps.get(action, _FALLBACK_CAP)
        with self._lock:
            stamps = self._load().get(action, [])
        now = self._now()
        in_hour = sum(1 for t in stamps if t > now - 3600)
        in_day = len(stamps)
        if in_hour >= hourly:
            return (f"that would be {in_hour + 1} '{action}' actions within an "
                    f"hour, past the safety limit of {hourly}")
        if in_day >= daily:
            return (f"that would be {in_day + 1} '{action}' actions today, past "
                    f"the safety limit of {daily}")
        return None

    def record(self, action: str) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(action, []).append(self._now())
            self._save(data)
