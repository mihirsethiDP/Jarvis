"""Speech-to-text with faster-whisper — fully offline."""

from __future__ import annotations

import numpy as np


class Transcriber:
    def __init__(self, model_size: str = "base.en", compute_type: str = "int8"):
        from faster_whisper import WhisperModel

        # First run downloads the model from Hugging Face; on proxy-locked
        # networks pre-download and pass a local directory path instead.
        self.model = WhisperModel(model_size, device="cpu", compute_type=compute_type)

    def transcribe(self, audio_f32_16k: np.ndarray) -> str:
        if audio_f32_16k.size == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio_f32_16k,
            beam_size=5,
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
