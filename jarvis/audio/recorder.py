"""Utterance recording with silence endpointing.

After the wake word fires, record until the user stops talking. Endpointing
is adaptive RMS-energy based (zero extra dependencies); faster-whisper's
built-in Silero VAD filter cleans up residual noise at transcription time.
"""

from __future__ import annotations

import numpy as np

from .microphone import Microphone
from .microphone import BLOCK_SIZE

_BLOCK_SECONDS = BLOCK_SIZE / 16000.0  # 32 ms
# Shorter than any real word; guards against a cough being treated as speech.
_SHORT_UTTERANCE_SECONDS = 0.18


class UtteranceRecorder:
    def __init__(
        self,
        mic: Microphone,
        *,
        max_seconds: float = 20.0,
        silence_seconds: float = 1.8,
        start_window_seconds: float = 6.0,
        min_speech_seconds: float = 0.3,
        detector=None,
    ):
        self.mic = mic
        self.max_seconds = max_seconds
        self.silence_seconds = silence_seconds
        self.start_window_seconds = start_window_seconds
        self.min_speech_seconds = min_speech_seconds
        # A voice-activity model when one is available; None falls back to the
        # RMS-energy path below, which cannot tell speech from a fan.
        self.detector = detector

    def record(self, start_window: float | None = None) -> np.ndarray:
        """Record one utterance; returns float32 mono 16 kHz audio (may be empty).

        The noise floor adapts only on *non-speech* blocks, and speech is
        checked from the very first block — so a user who starts talking
        immediately (one-breath "Hey Jarvis, what's…") is captured instead of
        being averaged into the noise floor.

        Args:
            start_window: How long to wait for speech to begin before giving
                up (defaults to the configured start window). The follow-up
                listener passes its own, shorter window here.
        """
        window = self.start_window_seconds if start_window is None else start_window
        if self.detector is not None:
            # The model carries state between frames; without this the tail of
            # the last utterance bleeds into the start of this one.
            self.detector.reset()
        blocks: list[np.ndarray] = []
        noise_rms = 150.0  # prior on int16 scale; adapts to the room below
        speech_started = False
        silence_run = 0.0
        speech_time = 0.0
        elapsed = 0.0

        while elapsed < self.max_seconds:
            block = self.mic.read(timeout=1.0)
            if block is None:
                elapsed += 1.0
                continue
            elapsed += _BLOCK_SECONDS
            rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))

            if self.detector is not None:
                # Asks the real question — "is anyone speaking?" — instead of
                # inferring it from loudness. A fan, a keyboard, or a
                # colleague's chair all clear an energy threshold; none of
                # them clear this one.
                is_speech = self.detector.is_speech(
                    block, already_speaking=speech_started
                )
            else:
                # Hysteresis: it takes a clear signal to *start* an utterance,
                # but much less to stay in one. Without this, the dips between
                # words — and the breath people take mid-sentence — read as
                # silence, and the recording ends while they are still talking.
                if speech_started:
                    threshold = max(noise_rms * 1.8, 180.0)
                else:
                    threshold = max(noise_rms * 3.0, 300.0)
                is_speech = rms >= threshold
            blocks.append(block)

            if is_speech:
                speech_started = True
                speech_time += _BLOCK_SECONDS
                silence_run = 0.0
            else:
                # Exponential floor tracking, updated only when not speaking.
                noise_rms = 0.9 * noise_rms + 0.1 * rms
                if speech_started:
                    silence_run += _BLOCK_SECONDS
                    # A short burst followed by a pause is someone drawing
                    # breath before the real sentence, not a finished request.
                    if (silence_run >= self.silence_seconds
                            and speech_time >= self.min_speech_seconds):
                        break
                    # ...but "yes", "haan" and "no" are complete utterances,
                    # and they are the most frequent thing anyone says to
                    # Jarvis. Requiring min_speech of them meant every single
                    # confirmation ran to the 20-second cap before
                    # transcription even started. A brief utterance followed
                    # by a markedly longer pause is finished.
                    if (speech_time >= _SHORT_UTTERANCE_SECONDS
                            and silence_run >= self.silence_seconds * 1.6):
                        break
                    # A blip too short to be a word, followed by a long
                    # silence, was a cough or a door. Give up rather than
                    # holding the microphone open to the cap for it.
                    if silence_run >= self.silence_seconds * 2.0:
                        return np.empty(0, dtype=np.float32)
                elif elapsed >= window:
                    return np.empty(0, dtype=np.float32)  # user never spoke

        if not speech_started or not blocks:
            return np.empty(0, dtype=np.float32)
        audio = np.concatenate(blocks).astype(np.float32) / 32768.0
        return audio
