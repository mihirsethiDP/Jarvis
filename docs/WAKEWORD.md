# Changing the wake word

**Short version: "Hey DP" cannot be switched on in config. It does not exist
yet as a model, and one has to be trained.** Everything below is how to get
there, and what to do in the meantime.

## Why it isn't a setting

Wake-word detection runs a small neural network on every 80 ms of audio. That
network is trained for one specific phrase. openWakeWord ships exactly six
pretrained ones:

```
alexa   hey_jarvis   hey_mycroft   hey_rhasspy   timer   weather
```

There is no "Hey DP" among them, and no amount of configuration produces one —
`audio.wake.model` selects a model, it does not define a phrase.

## First: find out whether the model hears you at all

Indian-accented speech scores lower against these models, because they were
trained mostly on American and British English. Before changing anything,
measure it:

```
jarvis wake-test
```

Say "Hey Jarvis" a few times at your normal distance and volume. Each attempt
prints its peak score against the 0.5 default:

| Peak | What it means | What to do |
|---|---|---|
| above 0.5 | Detection works | The problem is elsewhere — check the mic |
| 0.2 – 0.5 | The model hears you but scores low | Lower the threshold (below) |
| below 0.2 | It doesn't recognise your pronunciation | Train a custom word |

## The cheap fix: lower the threshold

If your peaks land in the 0.2–0.5 band, this is a two-line change and takes
effect immediately. In `%APPDATA%\Jarvis\config.yaml`:

```yaml
audio:
  wake:
    model: hey_jarvis
    threshold: 0.35      # default is 0.5
```

The trade-off is real: a lower threshold fires more easily on background
speech. Test it in a normal working room with people talking, not in a quiet
one — a threshold that behaves at your desk at 9pm may trigger through an
afternoon of conversation.

## The real fix: train "Hey DP"

openWakeWord has an automatic training pipeline. You never record yourself —
it synthesises thousands of variations of the phrase across many voices and
accents, mixes in noise and room reverb, and trains against large negative
speech corpora so it learns what *isn't* the wake word.

1. Open openWakeWord's training notebook (`automatic_model_training.ipynb`)
   in Google Colab — it needs a GPU, which Colab gives you free.
2. Set the target phrase. Generate several accent variants rather than one
   spelling, so the model covers how people actually say it:
   `hey dee pee`, `hey d p`, `hey deepee`.
3. Run the notebook. Expect roughly an hour end to end, most of it unattended.
4. Download the resulting `.onnx` file.

Install it:

```yaml
audio:
  wake:
    model: C:/Users/<you>/AppData/Roaming/Jarvis/models/hey_dp.onnx
    threshold: 0.5
```

`audio.wake.model` already accepts an absolute path, so no code change is
needed. Re-run `jarvis wake-test` against the new model and tune the
threshold the same way.

### One caveat about "Hey DP" specifically

It is a **short** phrase — three syllables, and "dee pee" is acoustically thin.
Short wake words false-trigger more, because there is less signal to
distinguish them from ordinary speech. Expect to spend real time on the
threshold, and consider training a longer alternative at the same time so you
can compare:

- `hey dee pee` — shortest, most false positives
- `hey digital paani` — noticeably more robust, clunkier to say
- `okay dee pee` — the extra syllable up front helps more than it looks

Train two and keep whichever behaves in a real room.

## The paid alternative

Picovoice Porcupine generates a custom wake word from typed text in about a
minute, with better accent robustness than openWakeWord out of the box. The
catch is licensing: the free tier is for personal and evaluation use, so
deploying it across DigitalPaani machines needs a commercial plan. If the
training route proves painful, this is the fallback worth pricing — but it is
a procurement decision, not a technical one.

openWakeWord is Apache-2.0 with no per-seat cost, which is why Jarvis uses it.
