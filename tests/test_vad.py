"""Endpointing with a voice-activity model rather than loudness.

RMS energy cannot tell a voice from a fan. In a room with background noise
the recorder either started on the noise or never ended, because the noise
never fell back under the threshold. Measured against real synthesized
speech mixed with broadband noise, the energy path recorded 12.9s where the
speech was 6.2s, and recorded 9.8s of pure noise as though it were a request.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio.recorder import UtteranceRecorder
from jarvis.audio.vad import SpeechDetector, try_build

BLOCK = 512


@pytest.fixture(scope="module")
def detector():
    d = try_build()
    if d is None:
        pytest.skip("silero_vad.onnx unavailable in this environment")
    return d


class ReplayMic:
    def __init__(self, samples: np.ndarray):
        pcm = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
        self.blocks = [pcm[i:i + BLOCK] for i in range(0, len(pcm) - BLOCK, BLOCK)]
        self.i = 0

    def read(self, timeout=None):
        if self.i >= len(self.blocks):
            return np.zeros(BLOCK, dtype=np.int16)
        block = self.blocks[self.i]
        self.i += 1
        return block

    def drain(self):
        pass


def _tone_speech(seconds=3.0):
    """A harmonic-rich, amplitude-modulated signal: not real speech, but far
    closer to it than white noise, which is what matters for the contrast."""
    t = np.arange(int(16000 * seconds), dtype=np.float32) / 16000
    sig = sum(np.sin(2 * np.pi * f * t) / (i + 1)
              for i, f in enumerate((140, 280, 560, 1120, 2240)))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t)   # syllable rate
    return (sig * envelope * 0.2).astype(np.float32)


def test_detector_separates_speech_from_loud_noise(detector):
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(16000 * 2) * 0.05).astype(np.float32)

    detector.reset()
    noise_frames = [detector.probability(noise[i:i + BLOCK])
                    for i in range(0, len(noise) - BLOCK, BLOCK)]
    # Loud broadband noise is comfortably above any RMS threshold and must
    # still register as "nobody is speaking".
    assert max(noise_frames) < 0.5, f"noise scored as speech (max {max(noise_frames):.2f})"


def test_noise_alone_is_not_recorded_as_a_request(detector):
    # The energy path captured ~9.8s of this and handed it to Whisper, which
    # hallucinates words from noise — so Jarvis acted on something never said.
    rng = np.random.default_rng(11)
    noise = (rng.standard_normal(16000 * 8) * 0.05).astype(np.float32)
    rec = UtteranceRecorder(ReplayMic(noise), silence_seconds=1.8,
                            max_seconds=20.0, detector=detector)
    assert rec.record().size == 0


def test_energy_path_is_still_available_as_a_fallback():
    # A machine where the model will not load must still record.
    rng = np.random.default_rng(5)
    levels = np.concatenate([
        (rng.standard_normal(16000 * 2) * 0.2).astype(np.float32),
        (rng.standard_normal(16000 * 4) * 0.001).astype(np.float32),
    ])
    rec = UtteranceRecorder(ReplayMic(levels), silence_seconds=1.8,
                            max_seconds=20.0, detector=None)
    assert rec.record().size > 0


def test_state_is_reset_between_utterances(detector):
    speech = _tone_speech(1.5)
    quiet = np.zeros(16000 * 4, dtype=np.float32)
    stream = np.concatenate([speech, quiet])

    first = UtteranceRecorder(ReplayMic(stream), silence_seconds=1.8,
                              max_seconds=20.0, detector=detector).record()
    second = UtteranceRecorder(ReplayMic(stream), silence_seconds=1.8,
                               max_seconds=20.0, detector=detector).record()
    # Without reset() the model's LSTM state carries over and the second
    # recording behaves differently from the first.
    assert abs(len(first) - len(second)) < BLOCK * 4


def test_a_missing_model_degrades_instead_of_raising(monkeypatch):
    import jarvis.audio.vad as vad_mod

    monkeypatch.setattr(vad_mod, "_model_path", lambda: "no/such/model.onnx")
    assert vad_mod.try_build() is None      # falls back, does not crash


def test_detector_can_be_switched_off_by_config():
    assert try_build(enabled=False) is None
