"""Live wake-word meter — see what your voice actually scores.

The pretrained models were trained mostly on American and British speech, so
an Indian-accented "Hey Jarvis" often peaks below the 0.5 default and the
assistant simply never wakes. There is no way to know without measuring, so
this prints the score for every frame and reports the peak per attempt.

Read it like this:

- peaks above 0.5      — detection is fine; the problem is elsewhere.
- peaks 0.2 to 0.5     — the model hears you; lower `audio.wake.threshold`.
- peaks below 0.2      — the model does not recognise your pronunciation.
                         A lower threshold would only buy false triggers;
                         a custom-trained wake word is the real fix.
"""

from __future__ import annotations

import time

import numpy as np

_FRAME_SAMPLES = 1280   # 80 ms at 16 kHz, what openWakeWord expects
_BAR_WIDTH = 40


def _bar(score: float, threshold: float) -> str:
    filled = int(round(min(score, 1.0) * _BAR_WIDTH))
    mark = int(round(threshold * _BAR_WIDTH))
    cells = []
    for i in range(_BAR_WIDTH):
        if i == mark:
            cells.append("|")          # the threshold line
        elif i < filled:
            cells.append("#")
        else:
            cells.append(".")
    return "".join(cells)


def run(model_name: str = "hey_jarvis", threshold: float = 0.5,
        seconds: int = 60) -> int:
    from .microphone import Microphone
    from .wake import WakeWordDetector

    print(f"\n=== Wake-word meter: {model_name} (threshold {threshold}) ===")
    print("Say the wake phrase a few times, in your normal voice and at your")
    print("normal distance. Ctrl+C to stop early.\n")

    peaks: list[float] = []
    run_peak = 0.0
    quiet_frames = 0
    buffer = np.empty(0, dtype=np.int16)
    deadline = time.monotonic() + seconds

    with Microphone() as mic:
        detector = WakeWordDetector(mic, model_name=model_name, threshold=threshold)
        model = detector.model
        try:
            while time.monotonic() < deadline:
                block = mic.read(timeout=1.0)
                if block is None:
                    continue
                buffer = np.concatenate([buffer, block])
                while len(buffer) >= _FRAME_SAMPLES:
                    frame, buffer = buffer[:_FRAME_SAMPLES], buffer[_FRAME_SAMPLES:]
                    score = max(model.predict(frame).values())

                    # Group consecutive non-silent frames into one "attempt" so
                    # the summary counts utterances, not frames.
                    if score > 0.05:
                        run_peak = max(run_peak, score)
                        quiet_frames = 0
                    else:
                        quiet_frames += 1
                        if run_peak > 0.05 and quiet_frames > 12:   # ~1s of quiet
                            peaks.append(run_peak)
                            print(f"    -> attempt peaked at {run_peak:.3f}"
                                  f"{'  DETECTED' if run_peak >= threshold else '  (missed)'}\n")
                            run_peak = 0.0

                    print(f"\r  {score:.3f}  {_bar(score, threshold)}", end="", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            print()

    if run_peak > 0.05:
        peaks.append(run_peak)

    if not peaks:
        print("\nNo speech registered. Check the microphone with `jarvis mic-test`.")
        return 1

    best = max(peaks)
    hits = sum(1 for p in peaks if p >= threshold)
    print(f"\n{len(peaks)} attempt(s); {hits} would have woken Jarvis. "
          f"Best score: {best:.3f}\n")

    if best >= threshold:
        print("Detection works at the current threshold.")
    elif best >= 0.2:
        suggested = round(max(best - 0.08, 0.15), 2)
        print(f"The model hears you but scores under the threshold. Try "
              f"`audio.wake.threshold: {suggested}` in config.yaml.\n"
              "Lower thresholds also mean more false triggers — test in a "
              "normal working room, not a silent one.")
    else:
        print("The model barely responds to your pronunciation. Lowering the\n"
              "threshold this far would fire on background speech. A wake word\n"
              "trained on your own voice is the real fix — see docs/WAKEWORD.md.")
    return 0
