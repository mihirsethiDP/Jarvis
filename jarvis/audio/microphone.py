"""Shared microphone stream.

One 16 kHz mono int16 input stream feeds every consumer (wake word,
endpointing). The sounddevice callback does nothing but copy blocks into a
queue — all inference happens on worker threads, so audio never overruns.
"""

from __future__ import annotations

import queue

import numpy as np
import sounddevice as sd

BLOCK_SIZE = 512  # 32 ms at 16 kHz — one VAD chunk; 2.5 blocks = one wake frame


class Microphone:
    def __init__(self, sample_rate: int = 16000, device: int | str | None = None):
        self.sample_rate = sample_rate
        self.device = device
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # drop a block rather than block the audio thread

    def start(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_SIZE,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float | None = None) -> np.ndarray | None:
        """Return the next 512-sample int16 block, or None on timeout.

        A timeout with a dead stream (headset unplugged, device switched)
        triggers a reopen attempt, so Jarvis recovers instead of going
        permanently deaf.
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            self._recover_if_dead()
            return None

    def _recover_if_dead(self) -> None:
        stream = self._stream
        if stream is not None and stream.active:
            return  # stream is fine — the room is just silent
        print("Microphone stream lost — attempting to reopen…")
        try:
            self.stop()
            self.start()
            print("Microphone recovered.")
        except Exception as e:
            self._stream = None
            print(f"Microphone unavailable ({e}); will retry.")

    def drain(self) -> None:
        """Discard everything buffered (e.g. audio captured while Jarvis spoke)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def __enter__(self) -> "Microphone":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
