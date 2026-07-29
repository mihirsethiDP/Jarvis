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


class UtteranceRecorder:
    def __init__(
        self,
        mic: Microphone,
        *,
        max_seconds: float = 20.0,
        silence_seconds: float = 1.2,
        start_window_seconds: float = 6.0,
    ):
        self.mic = mic
        self.max_seconds = max_seconds
        self.silence_seconds = silence_seconds
        self.start_window_seconds = start_window_seconds

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
        blocks: list[np.ndarray] = []
        noise_rms = 150.0  # prior on int16 scale; adapts to the room below
        speech_started = False
        silence_run = 0.0
        elapsed = 0.0

        while elapsed < self.max_seconds:
            block = self.mic.read(timeout=1.0)
            if block is None:
                elapsed += 1.0
                continue
            elapsed += _BLOCK_SECONDS
            rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))

            threshold = max(noise_rms * 3.0, 300.0)
            is_speech = rms >= threshold
            blocks.append(block)

            if is_speech:
                speech_started = True
                silence_run = 0.0
            else:
                # Exponential floor tracking, updated only when not speaking.
                noise_rms = 0.9 * noise_rms + 0.1 * rms
                if speech_started:
                    silence_run += _BLOCK_SECONDS
                    if silence_run >= self.silence_seconds:
                        break
                elif elapsed >= window:
                    return np.empty(0, dtype=np.float32)  # user never spoke

        if not speech_started or not blocks:
            return np.empty(0, dtype=np.float32)
        audio = np.concatenate(blocks).astype(np.float32) / 32768.0
        return audio
