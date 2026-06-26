# Boo Boo Kitty — Custom Wake Word Training

CofCITIP / JARVIS Phase 2, Session 7 (stretch).

This directory builds a **reproducible training environment** for a custom
OpenWakeWord wake word ("boo boo kitty"). It does **not** ship a trained model
— that requires Steven's recorded voice and is inherently manual and iterative.
What you get here is the container and the procedure to make training
straightforward once samples exist.

> **Reality check:** this is not a one-shot pipeline. You will record samples,
> train, measure false-positive / false-negative behavior on BB, and almost
> certainly retrain at least once or twice. Budget for iteration.

---

## Output format — read this first

`voice_listener.py` loads OpenWakeWord with `inference_framework="tflite"` and
passes the model from `WAKE_WORD_MODEL` into `wakeword_models=[...]`. So the
**target artifact is a `.tflite` file**, not `.onnx`. The ONNX graph is only an
intermediate that the OWW pipeline converts from — the reshape problem the
project notes mention lives in exactly that ONNX→TFLite conversion step, which
is why the Dockerfile pins (eventually) matter and why you verify the load
before trusting a model. See the Dockerfile header for the version-pinning TODO.

---

## 1. Record wake-word samples

You are recording yourself (and ideally anyone else who'll use voice) saying
**"boo boo kitty"**, the way you'll actually say it to JARVIS.

Rough guidance (not gospel — OWW's synthetic augmentation does heavy lifting,
but real clips anchor it):

- **Count:** on the order of **50–100+ clips** to start. More variety beats
  more repetition. If results are weak, record more rather than training longer.
- **Length:** ~**2–3 seconds** each. Say the phrase once, with a beat of silence
  before and after. Don't clip the start of the word.
- **Environment variety — record in BOTH:**
  - a **quiet room** (clean baseline), and
  - **ambient noise** variants — HVAC hum, hallway chatter, a fan, typing, the
    actual acoustic of wherever BB will live. Real deployments fire false
    positives on background noise; training on it is how you suppress them.
- **Delivery variety:** normal, slightly faster, slightly quieter, different
  distances from the mic. Match how you'll really talk to it.
- **Format:** mono WAV, 16 kHz if you can set it; if not, the container's
  ffmpeg/librosa will resample. Name them anything — they go in one folder.

Optional but recommended — **negative audio**: longer recordings of the room
*without* the wake phrase (and of similar-sounding phrases you do NOT want to
trigger on). These become hard negatives and cut false positives.

Put the files here on the host:

```
training/samples/      <- your "boo boo kitty" clips
training/negative/     <- (optional) background / near-miss audio
training/output/       <- trained model lands here
```

---

## 2. Build the training image

From the repo root (so the build context is `training/`):

```bash
cd training
docker build -t bbk-train .
```

First build is slow (TensorFlow is large). The image is pinned to
`python:3.10-slim-bookworm` for reproducibility — same image, same training
environment every time. **After your first successful train, lock the Python
package versions** per the TODO in the Dockerfile so the image is fully
reproducible, not just the base layer.

---

## 3. Run training

Mount your sample folders and an output folder into the container, then run
OpenWakeWord's training inside it. This is interactive on purpose — you want to
watch it and read the conversion step's output.

```bash
docker run --rm -it \
  -v "$(pwd)/samples:/samples" \
  -v "$(pwd)/negative:/negative" \
  -v "$(pwd)/output:/output" \
  bbk-train
```

Inside the container, drive OpenWakeWord's automatic training. The exact
invocation depends on the OWW version you pinned — check
`python -c "import openwakeword; print(openwakeword.__version__)"` and the
matching `notebooks/automatic_model_training*` / `openwakeword.train` docs.
Conceptually you are:

1. Pointing the trainer at `/samples` (positive) and `/negative` (negative).
2. Letting it synthesize + augment additional positives/negatives.
3. Training the classifier on top of OWW's shared feature embeddings.
4. **Converting the result to `.tflite`** and writing it to `/output`.

> **The conversion step is where the documented reshape bug bites.** When it
> finishes, do not assume success — verify in step 4.

Target a clear filename, e.g. `/output/boo_boo_kitty.tflite`.

---

## 4. Verify the model actually loads (don't skip this)

The reshape issue can produce a `.tflite` that exists but fails — or worse,
loads but never triggers. Sanity-check the load with the SAME framework
`voice_listener.py` uses, before deploying:

```bash
# inside the container, or on BB with openwakeword installed:
python - <<'PY'
from openwakeword.model import Model
m = Model(wakeword_models=["/output/boo_boo_kitty.tflite"],
          inference_framework="tflite")
print("loaded models:", list(m.models.keys()))
PY
```

If that prints your model key without error, the file is structurally good.
A reshape failure shows up here as a load/convert exception — fix conversion
(see Dockerfile pinning TODO) and retrain rather than shipping it.

---

## 5. Deploy to voice_listener.py

`voice_listener.py` needs **no code change** to use a custom model — it already
passes `WAKE_WORD_MODEL` straight into OpenWakeWord, and OWW accepts a file path
there. Two requirements:

1. The file must be **`.tflite`** (because `inference_framework="tflite"` is
   fixed in `voice_listener.py`).
2. Point config at it. Place the model where the `cofc-itip` service user can
   read it, e.g.:

   ```
   /var/lib/cofc-itip/models/boo_boo_kitty.tflite
   ```

   then in `config.env`:

   ```
   WAKE_WORD_MODEL=/var/lib/cofc-itip/models/boo_boo_kitty.tflite
   ```

   and restart the voice service:

   ```
   sudo systemctl restart jarvis-voice
   ```

**Do not change the default.** `hey_jarvis` stays the default in
`voice_listener.py` until you've trained and validated `boo_boo_kitty` and
decide to flip `WAKE_WORD_MODEL` yourself.

---

## 6. Iterate

Run it for real and watch two failure modes:

- **False negatives** (you say it, nothing wakes): record more positive variety,
  especially matching your real delivery/distance; consider lowering
  `WAKE_THRESHOLD` slightly.
- **False positives** (it wakes on noise / other speech): add the offending
  audio to `negative/`, retrain; consider raising `WAKE_THRESHOLD`.

Retrain, re-verify (step 4), redeploy (step 5). Repeat until the
false-positive/false-negative balance is acceptable in BB's actual room. There
is no "done" signal from the tooling — *you* decide when it's good enough.
