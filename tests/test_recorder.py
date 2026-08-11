"""Endpointing tests.

The failure these guard against was reported from real use: Jarvis "listens to
the first 2-4 words and does what it wants". The recorder was ending the
utterance on the pause people take mid-sentence, so the brain acted on a
fragment.
"""

from __future__ import annotations

import numpy as np

from jarvis.audio.recorder import UtteranceRecorder

BLOCK = 512                      # samples per mic block (32 ms at 16 kHz)
BLOCKS_PER_SECOND = 16000 / BLOCK


class FakeMic:
    """Replays a scripted sequence of loudness values as audio blocks."""

    def __init__(self, levels: list[float]):
        self.blocks = [
            (np.random.default_rng(i).standard_normal(BLOCK) * level)
            .astype(np.int16)
            for i, level in enumerate(levels)
        ]
        self.i = 0

    def read(self, timeout: float | None = None):
        if self.i >= len(self.blocks):
            return np.zeros(BLOCK, dtype=np.int16)   # room tone forever after
        block = self.blocks[self.i]
        self.i += 1
        return block

    def drain(self) -> None:
        pass


def seconds(n: float) -> int:
    return int(round(n * BLOCKS_PER_SECOND))


def test_pause_mid_sentence_does_not_end_the_utterance():
    # "Jarvis, can you..." <1.4s pause> "...check my email".
    # At the old 1.2s threshold this returned 2.43s — the opening fragment
    # only, which is exactly the "acts on the first few words" complaint.
    levels = ([2000] * seconds(1.2)      # first half of the sentence
              + [60] * seconds(1.4)      # thinking pause
              + [2000] * seconds(1.5)    # the rest of it
              + [40] * seconds(2.5))     # actually finished
    rec = UtteranceRecorder(FakeMic(levels), silence_seconds=1.8, min_speech_seconds=0.7)
    audio = rec.record()

    # Everything up to the trailing silence must survive, pause included.
    assert len(audio) / 16000 > 4.0, "recording was cut at the mid-sentence pause"


def test_recording_still_ends_after_a_real_stop():
    levels = [2000] * seconds(1.5) + [40] * seconds(3.0)
    rec = UtteranceRecorder(FakeMic(levels), silence_seconds=1.8, min_speech_seconds=0.7)
    audio = rec.record()
    # Ends shortly after the silence threshold rather than running to max_seconds.
    assert 3.0 < len(audio) / 16000 < 4.5


def test_a_brief_noise_burst_is_not_treated_as_a_finished_request():
    # A cough or a chair scrape, then the actual sentence a second later.
    levels = ([2500] * seconds(0.2)
              + [40] * seconds(1.0)
              + [2000] * seconds(1.4)
              + [40] * seconds(2.5))
    rec = UtteranceRecorder(FakeMic(levels), silence_seconds=1.8, min_speech_seconds=0.7)
    audio = rec.record()
    assert len(audio) / 16000 > 2.5, "gave up before the user actually spoke"


def test_silence_throughout_returns_nothing():
    rec = UtteranceRecorder(FakeMic([30] * seconds(8.0)), start_window_seconds=2.0)
    assert rec.record().size == 0


def test_max_seconds_is_still_enforced():
    rec = UtteranceRecorder(FakeMic([2000] * seconds(30)), max_seconds=4.0)
    audio = rec.record()
    assert len(audio) / 16000 <= 4.5


def test_a_one_word_yes_ends_promptly():
    # The most frequent utterance in the whole product. min_speech_seconds
    # meant a short "yes" never satisfied the end condition, so every single
    # confirmation ran to the 20-second cap before transcription even began.
    levels = [2200] * seconds(0.35) + [40] * seconds(8.0)
    rec = UtteranceRecorder(FakeMic(levels), silence_seconds=1.8, max_seconds=20.0)
    captured = len(rec.record()) / 16000
    assert captured < 4.0, f"a one-word answer took {captured:.1f}s to endpoint"


def test_a_cough_does_not_hold_the_microphone_open():
    levels = [2500] * seconds(0.12) + [40] * seconds(8.0)
    rec = UtteranceRecorder(FakeMic(levels), silence_seconds=1.8, max_seconds=20.0)
    assert rec.record().size == 0
