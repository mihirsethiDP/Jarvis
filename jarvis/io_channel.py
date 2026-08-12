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


class SwitchableIO:
    """Delegates to whichever channel the current turn came in on.

    The permission and confirmation gates are built once, with one channel.
    Without this, a request typed into the status page would be confirmed
    *out loud* and wait on the microphone — so someone who typed precisely
    because they could not speak would be stuck. Turns are serialised by the
    agent lock, so a single active channel is sufficient.
    """

    def __init__(self, default: IOChannel):
        self._default = default
        self._active: IOChannel | None = None

    def use(self, channel: IOChannel | None) -> None:
        self._active = channel

    @property
    def current(self) -> IOChannel:
        return self._active or self._default

    def say(self, text: str) -> None:
        self.current.say(text)

    def ask(self, prompt: str) -> str:
        return self.current.ask(prompt)


class WebIO:
    """Asks through the status page and blocks until the page answers.

    `ask` is called from the turn's worker thread while the HTTP server runs
    on its own; the Event is what makes the two meet.
    """

    def __init__(self, publish_say: Callable[[str], None],
                 publish_prompt: Callable[[str], None], timeout: float = 180.0):
        import threading

        self._say = publish_say
        self._prompt = publish_prompt
        self._timeout = timeout
        self._answered = threading.Event()
        self._answer = ""

    def say(self, text: str) -> None:
        self._say(text)

    def ask(self, prompt: str) -> str:
        self._answer = ""
        self._answered.clear()
        self._prompt(prompt)
        if not self._answered.wait(self._timeout):
            # Nobody answered. Returning empty means the gates fail closed,
            # exactly as silence does on the voice channel.
            return ""
        return self._answer

    def deliver(self, answer: str) -> None:
        """Called from the HTTP thread when the page replies."""
        self._answer = answer or ""
        self._answered.set()


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
