# The wake word: "Hey Riva"

**Decision (Sep 2026): DP Assistant's wake word is "Hey Riva".** Riva (Reva)
is the classical Sanskrit name of the Narmada river — and to an English ear
it sounds like "river". One name that works in Hindi and English, tied to
the water business, checked against the company directory for collisions
(none), and shaped like an assistant's name: two syllables, vowel ending.

**Status: trained and installed (28 Sep 2026).** The model lives at
`%APPDATA%\Jarvis\models\hey_riva.onnx`. Test scores on synthesized speech:
0.83-0.95 for "hey riva" in Indian-English voices (threshold 0.5), 0.00 for
unrelated phrases, "the river…", other names and "Alexa". Weak spots: Hindi-voice
pronunciations (0.02-0.32) and saying it run-together with the request; a
brief pause after "Hey Riva" (the chime) works reliably. Training metrics:
0.35 false triggers/hour, recall 0.45 on the deliberately hard synthetic set.
If real-voice scores from `jarvis wake-test` are low, retrain with real
recordings from the team added to the positive clips. Rollback: set
`audio.wake.model: alexa` (the pre-switch config is saved as
`config.yaml.bak-alexa`).

## Why it needs training

Wake-word detection runs a small neural network on every 80 ms of audio,
trained for one specific phrase. openWakeWord ships exactly six pretrained
ones:

```
alexa   hey_jarvis   hey_mycroft   hey_rhasspy   timer   weather
```

`audio.wake.model` selects a model; it cannot define a phrase. "Hey Riva"
has to be trained once — after that it is a file any DigitalPaani machine
can use.

## Training "Hey Riva" on Google Colab (~30–60 min, free GPU)

openWakeWord's automatic pipeline synthesises thousands of spoken variations
of the phrase across many voices, mixes in noise and room reverb, and trains
against large negative corpora so it learns what *isn't* the wake word. You
never record yourself.

1. Open the notebook `automatic_model_training.ipynb` from the openWakeWord
   GitHub repository (dscripka/openWakeWord, `notebooks/` folder) in Google
   Colab:
   https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb
   (If the link moves, search "openWakeWord automatic model training colab".)
2. Runtime → Change runtime type → **GPU** (T4 is fine on the free tier).
3. Set the target phrase to `hey riva`. Where the notebook accepts multiple
   spellings/pronunciations, add variants so it learns how the office will
   actually say it:
   - `hey riva`
   - `hey reeva`
   - `hey ree va`
4. Leave the defaults for sample counts and negative data unless the
   notebook suggests otherwise; run all cells. Most of the hour is
   unattended.
5. Download the resulting **`hey_riva.onnx`** (the `.tflite` twin is not
   needed — Windows runs the onnx one).

## Installing it

Drop the file in `Downloads` and tell the assistant's maintainer — or do it
by hand:

1. Copy `hey_riva.onnx` to `%APPDATA%\Jarvis\models\` (create the folder).
2. In `%APPDATA%\Jarvis\config.yaml`:

```yaml
audio:
  wake:
    model: C:/Users/<you>/AppData/Roaming/Jarvis/models/hey_riva.onnx
    threshold: 0.5
```

`audio.wake.model` accepts an absolute path — no code change needed. The
desktop widget and HUD derive their label from the filename and will show
**Say "Hey Riva"** automatically.

## Tuning after install

Measure before touching anything:

```
jarvis wake-test
```

Say "Hey Riva" a few times at normal distance and volume; each attempt
prints its peak score against the threshold.

| Peak | What it means | What to do |
|---|---|---|
| above 0.5 | Detection works | Done — test in a noisy room too |
| 0.2 – 0.5 | Hears you, scores low | Lower `threshold` toward 0.35 |
| below 0.2 | Doesn't recognise the pronunciation | Retrain with more pronunciation variants |

Test the final threshold in a normal working room with people talking, not a
quiet one — a threshold that behaves at 9 pm may trigger through an
afternoon of conversation. Note: barge-in (interrupting the assistant
mid-speech) runs at `threshold + 0.2`, so a threshold of 0.5 means barge-in
needs a clear 0.7 — that headroom is deliberate.

## The paid alternative

Picovoice Porcupine generates a custom wake word from typed text in about a
minute, with strong accent robustness out of the box. The catch is
licensing: the free tier is for personal and evaluation use, so deploying it
across DigitalPaani machines needs a commercial plan. If the training route
proves painful, this is the fallback worth pricing — a procurement decision,
not a technical one. openWakeWord is Apache-2.0 with no per-seat cost, which
is why DP Assistant uses it.
