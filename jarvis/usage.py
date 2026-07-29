"""Daily usage budget — a hard brake on API spend.

Every Claude call costs real money, and a bug, a stuck loop, or an
over-chatty employee shouldn't be able to burn through credits silently.
This is a simple, local, per-user counter: N brain turns per day, after
which Jarvis politely refuses until tomorrow. It is enforced in code before
the API is called — not a guideline the model could talk itself past.

(Defense in depth: pair this with a spend limit on the API key's workspace
in the Anthropic Console, which caps the blast radius even if this file's
logic were somehow bypassed.)
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from .paths import app_data_dir


def usage_file() -> Path:
    return app_data_dir() / "usage.json"


class TurnBudget:
    def __init__(
        self,
        daily_limit: int,
        path: Path | None = None,
        today: Callable[[], str] | None = None,
    ):
        self.daily_limit = daily_limit
        self.path = path or usage_file()
        self._today = today or (lambda: date.today().isoformat())
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"date": self._today(), "turns": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError, OSError):
            return {"date": self._today(), "turns": 0}
        if data.get("date") != self._today():
            return {"date": self._today(), "turns": 0}  # new day, fresh budget
        return data

    def used_today(self) -> int:
        with self._lock:
            return int(self._load().get("turns", 0))

    def allow(self) -> bool:
        if self.daily_limit <= 0:
            return True  # 0 = unlimited, deliberate opt-out
        return self.used_today() < self.daily_limit

    def record(self) -> None:
        """Count one attempted brain turn (attempted, not successful — a
        failed API call may still have cost tokens)."""
        with self._lock:
            data = self._load()
            data["turns"] = int(data.get("turns", 0)) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
