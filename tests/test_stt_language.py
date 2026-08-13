"""Language handling for Indian speech.

Reported from real use: a Hinglish request — "Yaar, Google chat pe message
bhejna hai Mansi Jain ko..." — was transcribed as English gibberish ("a Google
chart, same message, page, and I, man see Janko"). The sentence opens with
English loanwords, the detector votes English, and the Hindi half is mangled.

Two defences, both tested here with fake models:

1. Detection votes only among the languages the install expects, so
   code-switched speech cannot be dispersed across Urdu/Punjabi/Gujarati.
2. A wrong-language decode confesses in its own confidence (measured:
   correct -0.11..-0.46 avg_logprob, wrong -0.53..-1.80); below a line the
   audio is decoded again in the other language and the better result wins.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

import jarvis.audio.stt as stt_mod


class FakeSegment:
    def __init__(self, text, avg_logprob):
        self.text = text
        self.avg_logprob = avg_logprob


class FakeModel:
    """Scripted per-language decodes: {lang: (text, avg_logprob)}."""

    def __init__(self, decodes, detect=("en", 0.9, [("en", 0.9), ("hi", 0.1)])):
        self.decodes = decodes
        self.detect = detect
        self.decode_calls = []

    def transcribe(self, audio, language=None, **kw):
        self.decode_calls.append(language)
        text, lp = self.decodes.get(language, ("", -3.0))
        segs = [FakeSegment(text, lp)] if text else []
        return iter(segs), None

    def detect_language(self, audio):
        return self.detect


@pytest.fixture
def transcriber(monkeypatch):
    def build(decodes, detect):
        model = FakeModel(decodes, detect)
        fake_fw = types.SimpleNamespace(WhisperModel=lambda *a, **k: model)
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)
        tr = stt_mod.Transcriber(model_size="small", language="auto")
        tr._detector = model     # same fake serves detection
        return tr, model
    return build


AUDIO = np.ones(16000, dtype=np.float32)


def test_detection_votes_only_among_expected_languages(transcriber):
    # Whisper's 99-way choice disperses Hinglish across cousins; Punjabi wins
    # the global vote here, but only en/hi are languages this install can use.
    tr, model = transcriber(
        {"hi": ("यार, मैसेज भेजना है", -0.3)},
        detect=("pa", 0.5, [("pa", 0.5), ("hi", 0.3), ("en", 0.2)]),
    )
    assert tr.transcribe(AUDIO) == "यार, मैसेज भेजना है"
    assert model.decode_calls == ["hi"]


def test_a_rotten_decode_is_retried_in_the_other_language(transcriber):
    # The reported failure: detector says en, English decode is garbage.
    tr, model = transcriber(
        {"en": ("same message, page, and I, man see Janko", -1.4),
         "hi": ("मैसेज भेजना है मानसी जैन को", -0.35)},
        detect=("en", 0.8, [("en", 0.8), ("hi", 0.2)]),
    )
    assert tr.transcribe(AUDIO) == "मैसेज भेजना है मानसी जैन को"
    assert model.decode_calls == ["en", "hi"], "no retry happened"


def test_a_confident_decode_is_not_double_paid(transcriber):
    tr, model = transcriber(
        {"en": ("Send a message to Ranjana", -0.15)},
        detect=("en", 0.93, [("en", 0.93), ("hi", 0.02)]),
    )
    assert tr.transcribe(AUDIO) == "Send a message to Ranjana"
    assert model.decode_calls == ["en"], "a good decode must not trigger a retry"


def test_a_bad_retry_does_not_replace_a_less_bad_original(transcriber):
    tr, model = transcriber(
        {"en": ("mumbled but audible", -0.7),
         "hi": ("", -3.0)},
        detect=("en", 0.6, [("en", 0.6), ("hi", 0.4)]),
    )
    assert tr.transcribe(AUDIO) == "mumbled but audible"
