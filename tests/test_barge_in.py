"""Barge-in: saying the wake phrase over the assistant stops it talking.

Wake-word gated, deliberately: open VAD barge-in would hear the assistant's
own voice through the microphone and interrupt itself — that needs echo
cancellation this hardware doesn't have. A raised threshold covers the rest.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from jarvis.audio.wake import WakeWordDetector


class _Model:
    def __init__(self, scores):
        self._scores = list(scores)
        self.resets = 0

    def predict(self, frame):
        return {"alexa": self._scores.pop(0) if self._scores else 0.0}

    def reset(self):
        self.resets += 1


class _Mic:
    def read(self, timeout=None):
        return np.zeros(1280, dtype=np.int16)

    def drain(self):
        pass


def _detector(scores, threshold=0.5):
    det = WakeWordDetector.__new__(WakeWordDetector)
    det.mic = _Mic()
    det.threshold = threshold
    det.model = _Model(scores)
    return det


def test_raised_threshold_ignores_what_the_base_would_fire_on():
    # 0.6 clears the configured 0.5, but not the barge threshold — the
    # microphone is full of the assistant's own voice while this listens.
    det = _detector([0.6, 0.6, 0.0])
    stop_after = time.monotonic() + 0.3
    fired = det.wait(should_stop=lambda: time.monotonic() > stop_after,
                     threshold=0.7)
    assert not fired
    assert det.model.resets == 1, "a barge listener must clear model state on exit"


def test_a_clear_wake_phrase_fires_through_the_raised_threshold():
    det = _detector([0.2, 0.95])
    assert det.wait(threshold=0.7)


def test_stop_interrupts_between_chunks():
    from jarvis.audio.tts_edge import EdgeSpeaker

    class _Fallback:
        def say(self, text):
            pass

    spoken = []
    sp = EdgeSpeaker(fallback=_Fallback())
    # three sentences, long enough that say() takes the chunked path
    text = ("First sentence of a long reply here. "
            "Second sentence that should never play. "
            "Third sentence that should never play either.")

    def fake_say_one(chunk):
        spoken.append(chunk)
        sp.stop()          # the user barges in during the first chunk

    sp._say_one = fake_say_one
    sp.say(text)
    assert len(spoken) == 1, "everything after the interruption must be dropped"


def test_talk_button_stops_speech_instead_of_being_refused():
    from jarvis.app import JarvisApp

    stopped = []

    class _Speaker:
        def stop(self):
            stopped.append(True)

    app = JarvisApp.__new__(JarvisApp)
    app.voice = {"speaker": _Speaker()}
    app._speaking = True
    app._ui_busy = lambda: True
    assert JarvisApp._listen_from_ui(app) is True
    assert stopped == [True]

    # Busy but NOT speaking (a typed turn is thinking): still refused.
    app._speaking = False
    assert JarvisApp._listen_from_ui(app) is False


def test_speak_interruptible_arms_and_disarms_the_listener():
    from jarvis.app import JarvisApp

    events = []

    class _Wake:
        threshold = 0.5

        def wait(self, should_stop=None, threshold=None):
            events.append(("armed", threshold))
            while not should_stop():
                time.sleep(0.01)
            return False

    class _Speaker:
        def say(self, text):
            events.append(("spoke", text))

        def stop(self):
            events.append(("stopped",))

    class _Config:
        @staticmethod
        def get(key, default=None):
            return default

    app = JarvisApp.__new__(JarvisApp)
    app.config = _Config()
    app._publish = lambda *a, **k: None
    app._speaking = False
    JarvisApp._speak_interruptible(
        app, {"speaker": _Speaker(), "wake": _Wake()}, "The reply.")
    assert ("spoke", "The reply.") in events
    assert events[0] == ("armed", 0.7), "listener armed at base + 0.2"
    assert app._speaking is False


def test_reply_containing_the_wake_word_never_arms_the_listener():
    from jarvis.app import JarvisApp

    armed = []

    class _Wake:
        threshold = 0.5

        def wait(self, **kw):
            armed.append(True)
            return False

    class _Speaker:
        def say(self, text):
            pass

        def stop(self):
            pass

    class _Config:
        @staticmethod
        def get(key, default=None):
            return "alexa" if key == "audio.wake.model" else default

    app = JarvisApp.__new__(JarvisApp)
    app.config = _Config()
    app._publish = lambda *a, **k: None
    app._speaking = False
    JarvisApp._speak_interruptible(
        app, {"speaker": _Speaker(), "wake": _Wake()},
        "Alexa is the wake word you chose.")
    assert armed == [], "speaking the wake word must not trigger the assistant itself"
