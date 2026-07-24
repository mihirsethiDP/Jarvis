"""User interaction channels.

Every part of Jarvis that needs to talk to (or hear from) the user goes
through this small interface, so the security layer works identically in
voice mode and text mode.
"""

from __future__ import annotations

from typing import Callable, Protocol


class IOChannel(Protocol):
    def say(self, text: str) -> None:
        """Deliver a message to the user (speak it and/or print it)."""
        ...

    def ask(self, prompt: str) -> str:
        """Deliver a prompt and block until the user answers. Returns raw text."""
        ...


class TextIO:
    """Console-based channel — used in --text mode and in tests."""

    def say(self, text: str) -> None:
        print(f"\nJarvis: {text}")

    def ask(self, prompt: str) -> str:
        print(f"\nJarvis: {prompt}")
        # EOFError propagates: callers must distinguish "input channel closed"
        # from an empty answer (the setup wizard aborts; security gates deny).
        return input("You: ").strip()


class VoiceIO:
    """Speaks via TTS and listens via the microphone pipeline.

    Built from callables so audio modules stay swappable and this class
    stays import-safe on machines without audio dependencies installed.
    """

    def __init__(self, speak: Callable[[str], None], listen: Callable[[], str]):
        self._speak = speak
        self._listen = listen

    def say(self, text: str) -> None:
        print(f"\nJarvis: {text}")
        self._speak(text)

    def ask(self, prompt: str) -> str:
        self.say(prompt)
        heard = self._listen()
        print(f"You: {heard}")
        return heard
