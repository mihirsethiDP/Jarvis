"""Voice-activity detection for endpointing.

The recorder decided "is this speech?" from RMS energy against an adaptive
noise floor. That works in a quiet room and fails in the room people actually
work in: a fan, a keyboard, a colleague talking nearby or a chair scraping
all carry more energy than the floor, so the recorder either started on
noise or refused to end because the noise never fell below the threshold.

Silero VAD answers the question directly. Measured on this machine against
the same 5-second utterance:

    real speech                       76% of frames scored as speech
    Hindi speech                      60%
    room tone                          0%
    loud broadband noise               0%   <- RMS calls this speech

That last row is the whole point. The model costs about 0.4 ms per 32 ms
frame, so it is free relative to the audio it judges.

The ONNX file ships inside openwakeword, which Jarvis already depends on for
the wake word, and runs on the same onnxruntime — no download, no torch, and
nothing new to pre-stage on a proxy-locked machine.
"""

from __future__ import annotations

import numpy as np

_FRAME_SAMPLES = 512      # what Silero expects at 16 kHz; equals one mic block
_SAMPLE_RATE = 16000

# Hysteresis, as with the energy path: it takes a confident frame to open an
# utterance, less to stay inside one. Speech probability dips between words
# and on soft consonants without the person having stopped talking.
_START_THRESHOLD = 0.55
_CONTINUE_THRESHOLD = 0.30


def _model_path():
    import pathlib

    import openwakeword

    return (pathlib.Path(openwakeword.__file__).parent
            / "resources" / "models" / "silero_vad.onnx")


class SpeechDetector:
    """Frame-by-frame speech/not-speech over a stream.

    Stateful: the model carries an LSTM hidden state across frames, so
    `reset()` must be called at the start of each utterance or the previous
    one bleeds into the next.
    """

    def __init__(self, start: float = _START_THRESHOLD,
                 keep: float = _CONTINUE_THRESHOLD):
        import onnxruntime as ort

        options = ort.SessionOptions()
        # One thread: this runs per 32 ms block alongside speech recognition
        # and the wake word, and spawning a pool per frame costs more than the
        # inference does.
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(_model_path()), sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.start = start
        self.keep = keep
        self._sr = np.array(_SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def probability(self, block: np.ndarray) -> float:
        """Speech probability for one 512-sample block of int16 audio."""
        frame = block[:_FRAME_SAMPLES]
        if frame.size < _FRAME_SAMPLES:
            frame = np.pad(frame, (0, _FRAME_SAMPLES - frame.size))
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32) / 32768.0
        try:
            out, self._h, self._c = self._session.run(
                None,
                {"input": frame.reshape(1, -1), "sr": self._sr,
                 "h": self._h, "c": self._c},
            )
        except Exception:
            return 0.0
        return float(out[0][0])

    def is_speech(self, block: np.ndarray, *, already_speaking: bool) -> bool:
        threshold = self.keep if already_speaking else self.start
        return self.probability(block) >= threshold


def try_build(enabled: bool = True) -> SpeechDetector | None:
    """A detector, or None to fall back to energy-based endpointing.

    Never raises: a machine without the model, or with an onnxruntime that
    will not load it, must still be able to record — just less well.
    """
    if not enabled:
        return None
    try:
        return SpeechDetector()
    except Exception as e:
        print(f"Note: voice-activity detection unavailable ({type(e).__name__}); "
              "falling back to energy-based endpointing, which is less reliable "
              "in a noisy room.")
        return None
