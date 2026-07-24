"""Wake-word detection with openWakeWord.

Uses the pretrained "hey_jarvis" model on the ONNX runtime (Windows has no
tflite wheels, so the framework must be forced to onnx). Frames are 1280
samples (80 ms) of 16 kHz int16 audio.
"""

from __future__ import annotations

import time

import numpy as np

from .microphone import Microphone

_FRAME_SAMPLES = 1280
_REFRACTORY_SECONDS = 2.0


class WakeWordDetector:
    def __init__(
        self,
        mic: Microphone,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
    ):
        self.mic = mic
        self.threshold = threshold
        self.model = self._load_model(model_name)

    @staticmethod
    def _load_model(model_name: str):
        import openwakeword
        from openwakeword.model import Model

        try:
            return Model(wakeword_models=[model_name], inference_framework="onnx")
        except Exception:
            # First run on this machine: fetch the model + feature extractors.
            # (On proxy-locked networks, pre-bundle the .onnx files and point
            # audio.wake.model at an absolute path instead.)
            openwakeword.utils.download_models()
            return Model(wakeword_models=[model_name], inference_framework="onnx")

    def wait(self) -> None:
        """Block until the wake phrase is heard."""
        buffer = np.empty(0, dtype=np.int16)
        while True:
            block = self.mic.read(timeout=1.0)
            if block is None:
                continue
            buffer = np.concatenate([buffer, block])
            while len(buffer) >= _FRAME_SAMPLES:
                frame, buffer = buffer[:_FRAME_SAMPLES], buffer[_FRAME_SAMPLES:]
                scores = self.model.predict(frame)
                if max(scores.values()) >= self.threshold:
                    self.model.reset()
                    self.mic.drain()
                    time.sleep(0.05)
                    return
