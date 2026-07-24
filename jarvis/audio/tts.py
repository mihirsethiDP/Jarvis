"""Text-to-speech via Windows SAPI (pyttsx3) — offline and license-clean.

A fresh engine is created per utterance: pyttsx3's runAndWait loop is known
to wedge on reuse under SAPI5, and engine init is fast enough for a voice
assistant. Swap this class for a Piper/Kokoro implementation if the company
wants more natural voices (see README).
"""

from __future__ import annotations


class Speaker:
    def __init__(self, voice: str | None = None, rate: int = 180):
        self.voice = voice
        self.rate = rate

    def say(self, text: str) -> None:
        if not text.strip():
            return
        import pyttsx3

        engine = pyttsx3.init("sapi5")
        try:
            engine.setProperty("rate", self.rate)
            if self.voice:
                for v in engine.getProperty("voices"):
                    if self.voice.lower() in v.name.lower():
                        engine.setProperty("voice", v.id)
                        break
            engine.say(text)
            engine.runAndWait()
        finally:
            try:
                engine.stop()
            except Exception:
                pass
