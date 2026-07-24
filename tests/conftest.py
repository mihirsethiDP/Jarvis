from __future__ import annotations

import pytest

from jarvis.security.audit import AuditLog


class FakeIO:
    """Scripted IO channel for tests: answers questions from a queue."""

    def __init__(self, answers: list[str] | None = None):
        self.answers = list(answers or [])
        self.said: list[str] = []
        self.asked: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError  # mirrors a closed console (TextIO propagates it)
        return self.answers.pop(0)


@pytest.fixture
def audit(tmp_path):
    return AuditLog(path=tmp_path / "audit.jsonl")
