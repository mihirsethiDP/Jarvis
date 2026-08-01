"""Speech-to-text with faster-whisper — fully offline.

Latency matters more here than anywhere else in Jarvis: this sits between the
user finishing a sentence and anything at all happening. Measured on the
target machine (12 cores, int8, a 5-second utterance, model "small"):

    language="auto", beam_size=5      13.9 s     <- what shipped first
    language="auto", beam_size=1      14.8 s     beam size is not the problem
    language pinned, fast decode       6.3 s
    language detection alone, "small"  8.4 s     <- the actual cost
    language detection alone, "tiny"   1.7 s     same answer, 0.92 confidence

So "auto" was spending more time deciding *which* language it was hearing
than transcribing it. Detection runs on a tiny model instead, every utterance
rather than cached, because a wrong sticky language mangles a whole reply and
1.7 s is not worth that risk.
"""

from __future__ import annotations

import os

import numpy as np

# Aliases with a real multilingual twin ("base.en" -> "base"). distil-* and
# local directory paths have no such twin and must not be blindly stripped.
_MULTILINGUAL_TWINS = {"tiny", "base", "small", "medium"}

# Detection only ever looks at the first mel window; feeding it more is waste.
_DETECT_SECONDS = 12
_DETECT_MODEL = "tiny"
_MIN_CONFIDENCE = 0.45   # below this, let the full model decide for itself

# Decoding options measured as free speedups: greedy search matched beam
# search word-for-word on these utterances, and neither timestamps nor
# cross-utterance conditioning is used downstream.
_DECODE = {
    "beam_size": 1,
    "condition_on_previous_text": False,
    "without_timestamps": True,
    "vad_filter": True,
    "vad_parameters": {"min_silence_duration_ms": 500},
}


class Transcriber:
    def __init__(
        self,
        model_size: str = "base.en",
        compute_type: str = "int8",
        language: str = "en",
        cpu_threads: int = 0,
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
        self._auto = language == "auto"
        self._detector = None
        self._detector_failed = False

        # Left at 0, ctranslate2 picks a conservative default; the measured
        # difference on this machine was a model load of 44s versus 7s.
        self._cpu_threads = cpu_threads or min(8, (os.cpu_count() or 4))

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
        self.model = WhisperModel(
            model_size, device="cpu", compute_type=compute_type,
            cpu_threads=self._cpu_threads,
        )
        self._compute_type = compute_type

    # ------------------------------------------------------------------
    def _detect_language(self, audio: np.ndarray) -> str | None:
        """Best-guess language from a small, cheap model.

        Returns None to mean "don't pin it" — the caller then falls back to
        the full model's own detection, which is slow but never worse.
        """
        if self._detector_failed:
            return None
        if self._detector is None:
            try:
                from faster_whisper import WhisperModel

                self._detector = WhisperModel(
                    _DETECT_MODEL, device="cpu", compute_type=self._compute_type,
                    cpu_threads=self._cpu_threads,
                )
            except Exception:
                # No detector: correctness is unaffected, only speed.
                self._detector_failed = True
                return None
        try:
            clip = audio[: _DETECT_SECONDS * 16000]
            language, probability, *_ = self._detector.detect_language(clip)
        except Exception:
            self._detector_failed = True
            return None
        if probability < _MIN_CONFIDENCE:
            return None
        return language

    def transcribe(self, audio_f32_16k: np.ndarray) -> str:
        if audio_f32_16k.size == 0:
            return ""
        language = self.language
        if self._auto:
            language = self._detect_language(audio_f32_16k)
        segments, _info = self.model.transcribe(
            audio_f32_16k, language=language, **_DECODE
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
