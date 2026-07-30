"""Neural text-to-speech via Microsoft Edge voices (edge-tts).

Natural-sounding Hindi/Hinglish and Indian-English voices, chosen per reply:
Devanagari text speaks with the Hindi voice, everything else with the English
one (both hi-IN/en-IN voices handle mixed Hinglish sentences well).

PRIVACY NOTE: edge-tts is a **cloud** service — every reply Jarvis speaks is
sent to Microsoft's servers for synthesis. This was an explicit deployment
decision (see docs/SECURITY.md). On any failure it falls back to the offline
SAPI voice so the assistant never goes mute.
"""

from __future__ import annotations

import asyncio
import time

from .tts import Speaker

_DEVANAGARI_START, _DEVANAGARI_END = "ऀ", "ॿ"
# Generous on purpose: office wifi and VPNs routinely stall a second or two,
# and every timeout here costs the user the Indian voice and hands them the
# American Windows one instead. Waiting is the lesser annoyance.
_SYNTH_TIMEOUT = 25.0     # total budget incl. DNS; long replies still fit
_COOLOFF_SECONDS = 120.0  # after repeated failures, back off — then re-probe


def _looks_hindi(text: str) -> bool:
    return any(_DEVANAGARI_START <= ch <= _DEVANAGARI_END for ch in text)


class EdgeSpeaker:
    def __init__(
        self,
        voice_en: str = "en-IN-NeerjaNeural",
        voice_hi: str = "hi-IN-SwaraNeural",
        fallback: Speaker | None = None,
        on_engine=None,
    ):
        self.voice_en = voice_en
        self.voice_hi = voice_hi
        self.fallback = fallback or Speaker()
        # Called with ("edge"|"offline", reason) after each utterance, so the
        # status page can say *why* the accent changed. Losing the Indian
        # voice silently is what makes this look like a broken setting.
        self.on_engine = on_engine
        self.last_engine = "edge"
        self._failures = 0
        self._cooloff_until = 0.0

    def _use_fallback(self, text: str, reason: str) -> None:
        if self.last_engine != "offline":
            print(f"(Indian voice unavailable — {reason}. "
                  "Using the offline Windows voice, which is US English.)")
        self.last_engine = "offline"
        if self.on_engine:
            self.on_engine("offline", reason)
        self.fallback.say(text)

    def say(self, text: str) -> None:
        if not text.strip():
            return
        # Circuit breaker: on a blocked/plant network, don't stall every
        # utterance on a dead cloud — go straight offline for a cool-off,
        # then probe once.
        if self._failures >= 2 and time.monotonic() < self._cooloff_until:
            self._use_fallback(text, "still retrying the connection")
            return

        # One retry before giving up the good voice: most failures here are a
        # single dropped connection, not an unreachable service.
        last_error = None
        for _ in range(2):
            try:
                mp3 = asyncio.run(
                    asyncio.wait_for(self._synthesize(text), timeout=_SYNTH_TIMEOUT)
                )
                self._play(mp3)
                self._failures = 0
                if self.last_engine != "edge" and self.on_engine:
                    self.on_engine("edge", "recovered")
                self.last_engine = "edge"
                return
            except Exception as e:
                last_error = e

        self._failures += 1
        if self._failures >= 2:
            self._cooloff_until = time.monotonic() + _COOLOFF_SECONDS
        self._use_fallback(text, f"{type(last_error).__name__}: {last_error}")

    async def _synthesize(self, text: str) -> bytes:
        import edge_tts

        voice = self.voice_hi if _looks_hindi(text) else self.voice_en
        communicate = edge_tts.Communicate(
            text, voice, connect_timeout=8, receive_timeout=20
        )
        chunks: list[bytes] = []
        async for message in communicate.stream():
            if message["type"] == "audio":
                chunks.append(message["data"])
        if not chunks:
            raise RuntimeError("no audio returned")
        return b"".join(chunks)

    @staticmethod
    def _play(mp3_bytes: bytes) -> None:
        import miniaudio
        import numpy as np
        import sounddevice as sd

        decoded = miniaudio.decode(mp3_bytes)  # PCM int16
        samples = np.asarray(decoded.samples, dtype=np.int16)
        if decoded.nchannels > 1:
            samples = samples.reshape(-1, decoded.nchannels)
        sd.play(samples, samplerate=decoded.sample_rate, blocking=True)
