# training/Dockerfile — Boo Boo Kitty custom wake word training
# CofCITIP | JARVIS Phase 2, Session 7 (stretch)
#
# WHY DOCKER, NOT COLAB:
#   OpenWakeWord's official training path is a Colab notebook with documented
#   instability (kernel resets mid-train, and an undocumented ONNX->TFLite
#   reshape step that silently produces a model the inference side won't load).
#   A pinned container is reproducible: same image -> same training environment
#   every run, which is the entire point of putting this in a Dockerfile.
#
# WHAT THIS PRODUCES:
#   An image that, given recorded wake-word samples (see training/README.md),
#   runs OpenWakeWord's automatic-model-training pipeline and emits a model
#   file. voice_listener.py loads with inference_framework="tflite", so the
#   TARGET OUTPUT FORMAT IS .tflite (not .onnx). See README "Output format".

# ── Base image ────────────────────────────────────────────────────────────────
# Pinned to 3.10-slim-bookworm, NOT python:latest. OpenWakeWord's training
# extras depend on the TensorFlow / tflite-runtime ecosystem, which historically
# lags newest CPython. 3.10 is the most broadly-tested interpreter for the
# OWW training toolchain at time of writing. Pinning the Debian release
# (bookworm) too, so the apt-provided audio libs don't drift between builds.
FROM python:3.10-slim-bookworm

# Fail loudly, no interactive apt prompts, no .pyc clutter, unbuffered logs.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── System audio / build deps ─────────────────────────────────────────────────
# libsndfile1  : soundfile / librosa read+write of the recorded .wav samples
# ffmpeg       : resample / decode arbitrary input audio to 16kHz mono
# build-essential, git : some training deps build native wheels / clone helpers
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
      libsndfile1 \
      ffmpeg \
      build-essential \
      git \
    && rm -rf /var/lib/apt/lists/*

# ── Python training stack ─────────────────────────────────────────────────────
# IMPORTANT — VERSION PINS AND THE ONNX->TFLite RESHAPE ISSUE:
#
#   The project notes flag an "undocumented ONNX->TFLite reshape problem" in
#   OpenWakeWord training. I do NOT have confident, verified knowledge of the
#   EXACT package-version combination that is known to avoid it. Rather than
#   invent version pins that might be wrong (a wrong pin here is worse than an
#   honest gap — it produces a model that loads but misbehaves, or fails to
#   convert with a confusing reshape error), the pins below are left as
#   PLACEHOLDERS for Steven to lock against whatever combination actually
#   produces a working .tflite on BB's toolchain.
#
#   HOW TO LOCK THESE (do this once, on the first successful train):
#     1. Build with the loose ranges below.
#     2. Run a full train + convert + load-test in voice_listener (see README).
#     3. If it works: run `pip freeze` INSIDE the container and copy the exact
#        versions of openwakeword, tensorflow, tflite-runtime/ai-edge-litert,
#        onnx, and onnx2tf/onnxruntime into the pins here. Rebuild. That image
#        is now your reproducible training environment — commit the frozen pins.
#     4. If conversion fails with a reshape/transpose error: the usual culprit
#        is a tensorflow <-> onnx-converter version mismatch. Pin tensorflow
#        first, then find the onnx2tf release notes matching that TF minor.
#
#   Known-relevant packages (names, not verified versions):
#     - openwakeword[train]  : the training extras (data gen + model fit)
#     - tensorflow           : training backend AND the tflite converter source
#     - onnx, onnxruntime    : intermediate graph the OWW pipeline emits
#     - onnx2tf  (or tf2onnx/onnx-tf, depending on OWW's pipeline direction)
#                              : the ONNX<->TF bridge where the reshape bug bites
#     - ai-edge-litert / tflite-runtime : tflite conversion + load verification
#     - librosa, soundfile, scipy, numpy : audio feature prep
#
#   >>> EDIT THESE PINS AFTER YOUR FIRST GREEN RUN. They are deliberately loose
#   >>> now so the image builds; they are NOT yet reproducible. <<<
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
      "openwakeword[train]" \
      tensorflow \
      onnx \
      onnxruntime \
      onnx2tf \
      librosa \
      soundfile \
      scipy \
      numpy

# ── Pre-cache OWW base/feature models ─────────────────────────────────────────
# OWW training needs the shared feature-extractor (melspectrogram + embedding)
# base models. Fetch them at build time so a training run is fully offline and
# doesn't fail mid-train on a network hiccup. Tolerate failure (some OWW
# versions fetch lazily) — don't break the image build over it.
RUN python -c "import openwakeword.utils as u; u.download_models()" || \
    echo "WARN: base model pre-cache failed; OWW will fetch on first train run"

# ── Workspace layout ──────────────────────────────────────────────────────────
# /samples  : (bind-mount) Steven's recorded "boo boo kitty" wav clips
# /negative : (bind-mount, optional) background/negative audio for hard negatives
# /output   : (bind-mount) where the trained boo_boo_kitty.tflite lands
WORKDIR /train
RUN mkdir -p /samples /negative /output

# No ENTRYPOINT that hides the command — training is an interactive, iterative
# process (see README). Drop into a shell by default so the operator runs the
# OWW training invocation explicitly and watches it.
CMD ["/bin/bash"]
