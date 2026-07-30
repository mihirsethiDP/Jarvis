"""Audition the Indian neural voices and pick one.

Jarvis speaks with `en-IN-NeerjaNeural` / `hi-IN-SwaraNeural` by default —
both Indian. If it sounds American, edge-tts failed and the **offline** SAPI
fallback took over, which on a stock Windows install means Microsoft David or
Zira (both US English). That is a network symptom, not a voice setting, so
this module reports it rather than letting you chase the wrong knob.
"""

from __future__ import annotations

import asyncio

# Every en-IN / hi-IN voice edge-tts offers. Hindi voices are used only for
# Devanagari text; Hinglish in Latin script goes to the English voice, which
# handles it well.
VOICES = [
    ("en-IN-NeerjaNeural",           "female", "Indian English — warm, even. Current default."),
    ("en-IN-NeerjaExpressiveNeural", "female", "Indian English — livelier, more range."),
    ("en-IN-PrabhatNeural",          "male",   "Indian English — calm and measured."),
    ("hi-IN-SwaraNeural",            "female", "Hindi — current default for Devanagari."),
    ("hi-IN-MadhurNeural",           "male",   "Hindi — deeper, unhurried."),
]

_SAMPLE_EN = ("Good morning. You have two meetings today, and the Nashik plant "
              "report is ready for review.")
_SAMPLE_HI = "नमस्ते, मैं जार्विस हूँ। कल की मीटिंग ग्यारह बजे तय है।"


async def _synthesize(text: str, voice: str) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, connect_timeout=5, receive_timeout=15)
    chunks: list[bytes] = []
    async for message in communicate.stream():
        if message["type"] == "audio":
            chunks.append(message["data"])
    if not chunks:
        raise RuntimeError("no audio returned")
    return b"".join(chunks)


def _play(mp3_bytes: bytes) -> None:
    import miniaudio
    import numpy as np
    import sounddevice as sd

    decoded = miniaudio.decode(mp3_bytes)
    samples = np.asarray(decoded.samples, dtype=np.int16)
    if decoded.nchannels > 1:
        samples = samples.reshape(-1, decoded.nchannels)
    sd.play(samples, samplerate=decoded.sample_rate, blocking=True)


def audition(only: str | None = None) -> int:
    """Speak a sample line in each Indian voice, then show how to keep one."""
    picks = [v for v in VOICES if only is None or v[0] == only]
    if not picks:
        print(f"No such voice: {only}\nAvailable: " + ", ".join(v[0] for v in VOICES))
        return 1

    print("\n=== Indian voices (edge-tts) ===")
    print("Each one speaks a sample. Ctrl+C to stop.\n")

    failures = 0
    for name, gender, note in picks:
        text = _SAMPLE_HI if name.startswith("hi-IN") else _SAMPLE_EN
        print(f"  {name:<30} {gender:<7} {note}")
        try:
            _play(asyncio.run(_synthesize(text, name)))
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as e:
            failures += 1
            print(f"      ! could not synthesize: {type(e).__name__}: {e}")

    if failures == len(picks):
        print(
            "\nEvery voice failed, which means edge-tts is unreachable from this "
            "machine — a proxy or firewall is the usual cause. That is exactly "
            "when Jarvis falls back to the offline Windows voice and starts "
            "sounding American. Fix the network path and the Indian voices return."
        )
        return 1

    print(
        "\nTo keep one, put it in %APPDATA%\\Jarvis\\config.yaml:\n\n"
        "  audio:\n"
        "    tts:\n"
        "      engine: edge\n"
        "      edge_voice_en: en-IN-PrabhatNeural   # any en-IN voice above\n"
        "      edge_voice_hi: hi-IN-MadhurNeural    # any hi-IN voice above\n"
    )
    return 0
