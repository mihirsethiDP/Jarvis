"""The wake phrases available to choose from.

openWakeWord ships six pretrained models, but only four are wake *phrases* —
`timer` and `weather` are command classifiers ("set a timer"), not names you
would call an assistant by, and selecting one would leave Jarvis triggering
on ordinary sentences.

Nothing here can invent a new phrase. Each model is a small neural network
trained for one specific utterance; "Hey DP" needs a model trained for it
(see docs/WAKEWORD.md). This module only lists what already exists.
"""

from __future__ import annotations

# (model name, what you say, notes that actually matter when choosing)
WAKE_WORDS = [
    ("hey_jarvis", "Hey Jarvis",
     "Three syllables and the current default. Best-trained of the set, but "
     "the one an Indian-accented speaker most often has to repeat."),
    ("alexa", "Alexa",
     "Very well trained and reliably detected. Bad choice if there is an "
     "Echo device anywhere nearby — you will trigger both."),
    ("hey_mycroft", "Hey Mycroft",
     "Distinctive, so it rarely fires by accident. Awkward to say aloud in "
     "an office and unfamiliar to most people."),
    ("hey_rhasspy", "Hey Rhasspy",
     "Least likely of the four to false-trigger, because nothing else sounds "
     "like it. Hardest to pronounce consistently."),
]

# Shipped by openWakeWord but not wake phrases — listed so the choice is
# informed rather than mysterious.
NOT_WAKE_WORDS = {
    "timer": "detects the phrase 'set a timer', not a name",
    "weather": "detects a weather question, not a name",
}


def available() -> list[str]:
    return [name for name, _, _ in WAKE_WORDS]


def describe(current: str = "") -> str:
    lines = ["", "=== Wake phrases you can choose ===", ""]
    for name, spoken, note in WAKE_WORDS:
        mark = "  <- currently set" if name == current else ""
        lines.append(f'  {name:<13} say "{spoken}"{mark}')
        lines.append(f'  {"":<13} {note}')
        lines.append("")
    lines += [
        "To change it, set this in %APPDATA%\\Jarvis\\config.yaml:",
        "",
        "  audio:",
        "    wake:",
        "      model: alexa",
        "",
        "Then restart Jarvis. Measure how well it hears you with:",
        "",
        "  jarvis wake-test",
        "",
        "If none of these is detected reliably in your voice, the answer is a",
        "model trained for your own phrase rather than a different one of these",
        "— see docs/WAKEWORD.md. You can also skip the wake word entirely and",
        "press Talk on the status page.",
        "",
        "Also shipped, but NOT wake phrases (they detect a request, not a name):",
    ]
    for name, why in NOT_WAKE_WORDS.items():
        lines.append(f"  {name:<13} {why}")
    lines.append("")
    return "\n".join(lines)
