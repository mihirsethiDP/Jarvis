"""Speech-to-text with faster-whisper — fully offline."""

from __future__ import annotations

import os

import numpy as np

# Aliases with a real multilingual twin ("base.en" -> "base"). distil-* and
# local directory paths have no such twin and must not be blindly stripped.
_MULTILINGUAL_TWINS = {"tiny", "base", "small", "medium"}


class Transcriber:
    def __init__(
        self,
        model_size: str = "base.en",
        compute_type: str = "int8",
        language: str = "en",
    ):
        from faster_whisper import WhisperModel

        # language: "en", "hi", … or "auto" (detect per utterance — handles
        # Hinglish code-switching best). Fail at startup with a clear message
        # rather than erroring on every utterance later.
        if language != "auto":
            try:
                from faster_whisper.tokenizer import _LANGUAGE_CODES

                if language not in _LANGUAGE_CODES:
                    raise ValueError(
                        f"audio.stt.language '{language}' is not a Whisper language "
                        "code (e.g. en, hi) — or use 'auto'."
                    )
            except ImportError:
                pass  # internal API moved — skip validation rather than break
        self.language: str | None = None if language == "auto" else language

        # Non-English needs a multilingual model; upgrade known ".en" aliases.
        if language != "en" and model_size.endswith(".en") and not os.path.isdir(model_size):
            twin = model_size.removesuffix(".en")
            if twin not in _MULTILINGUAL_TWINS:
                raise ValueError(
                    f"Model '{model_size}' is English-only and has no multilingual "
                    f"twin. Set audio.stt.model_size to one of {sorted(_MULTILINGUAL_TWINS)} "
                    f"(or a multilingual local model path) for language={language!r}."
                )
            print(f"Note: language={language!r} needs the multilingual Whisper model "
                  f"'{twin}' — first run downloads it (~a few hundred MB).")
            model_size = twin
        elif language != "en" and os.path.isdir(model_size) and model_size.endswith(".en"):
            raise ValueError(
                f"Local model '{model_size}' is English-only; point "
                f"audio.stt.model_size at a multilingual model directory for "
                f"language={language!r}."
            )
        # First run downloads the model from Hugging Face; on proxy-locked
        # networks pre-download and pass a local directory path instead.
        self.model = WhisperModel(model_size, device="cpu", compute_type=compute_type)

    def transcribe(self, audio_f32_16k: np.ndarray) -> str:
        if audio_f32_16k.size == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio_f32_16k,
            beam_size=5,
            language=self.language,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
