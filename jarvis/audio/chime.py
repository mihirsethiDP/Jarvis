"""Short tones that tell the user the microphone is open.

Nothing signalled that Jarvis was listening. After the wake word the user got
silence, spoke into a void, and could not tell the difference between "the
wake word fired and I am recording you" and "nothing happened" — which is
most of what "sometimes it doesn't listen" feels like from the outside.

A tone rather than a spoken "yes?": speech synthesis is a cloud round-trip
costing a second or more, and this has to land the instant recording starts
or it is not a cue at all. These are generated locally, in a few
milliseconds, and played non-blocking so they never delay the recording they
are announcing.
"""

from __future__ import annotations

_SAMPLE_RATE = 16000

# (frequency Hz, seconds) pairs. Rising = I am listening; falling = finished.
_PATTERNS = {
    "listening": [(660, 0.06), (990, 0.07)],
    "done": [(880, 0.05), (590, 0.06)],
    "error": [(400, 0.10)],
}


def play(kind: str = "listening") -> None:
    """Play a cue. Never raises and never blocks the caller."""
    pattern = _PATTERNS.get(kind)
    if pattern is None:
        return
    try:
        import numpy as np
        import sounddevice as sd

        segments = []
        for freq, seconds in pattern:
            n = int(_SAMPLE_RATE * seconds)
            t = np.arange(n, dtype=np.float32) / _SAMPLE_RATE
            tone = np.sin(2 * np.pi * freq * t)
            # Fade both ends, otherwise the discontinuity clicks audibly.
            fade = max(1, n // 8)
            envelope = np.ones(n, dtype=np.float32)
            envelope[:fade] = np.linspace(0.0, 1.0, fade)
            envelope[-fade:] = np.linspace(1.0, 0.0, fade)
            segments.append((tone * envelope * 0.18).astype(np.float32))
        sd.play(np.concatenate(segments), samplerate=_SAMPLE_RATE, blocking=False)
    except Exception:
        # A cue is a nicety; a machine with no output device must still work.
        pass
