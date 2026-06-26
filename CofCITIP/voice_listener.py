"""
voice_listener.py — JARVIS Voice Pipeline
CofCITIP | College of Charleston IT

Wake word: OpenWakeWord (fully offline, no API key)
STT:       Whisper (local via faster-whisper)
TTS:       GLaDOS

Wake word model: hey_jarvis (Phase 1 default)
                 Boo Boo Kitty custom model (Phase 2 — train via OpenWakeWord training repo)

Requires:
    pip install openwakeword pyaudio numpy faster-whisper
    GLaDOS installed from source at /opt/cofc-itip/GlaDOS

Environment (config.env):
    WAKE_WORD_MODEL   — openwakeword model name (default: hey_jarvis)
    WAKE_THRESHOLD    — detection confidence threshold (default: 0.5)
    WHISPER_MODEL     — faster-whisper model size (default: base.en)
    JARVIS_CORE_URL   — internal HTTP endpoint for jarvis_core (default: http://127.0.0.1:8081)
    AUDIO_DEVICE_INDEX — pyaudio input device index (optional, defaults to system default)
"""

import os
import sys
import numpy as np
import pyaudio
import requests
import structlog

from openwakeword.model import Model as WakeWordModel
from faster_whisper import WhisperModel

# ── Logging ───────────────────────────────────────────────────────────────────

# Session 5: JSON renderer — runs under systemd; journald gets structured
# logs like every other JARVIS component. (print() calls in speak() remain
# deliberately: they are the TTS-unavailable fallback UI, not logging.)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────────────────────

WAKE_WORD_MODEL   = os.getenv("WAKE_WORD_MODEL", "hey_jarvis")
WAKE_THRESHOLD    = float(os.getenv("WAKE_THRESHOLD", "0.5"))
WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "base.en")
JARVIS_CORE_URL   = os.getenv("JARVIS_CORE_URL", "http://127.0.0.1:8081")  # Session 5: core moved to 8081
AUDIO_DEVICE_INDEX = os.getenv("AUDIO_DEVICE_INDEX")
if AUDIO_DEVICE_INDEX is not None:
    AUDIO_DEVICE_INDEX = int(AUDIO_DEVICE_INDEX)

# Audio constants — OpenWakeWord requires 16kHz mono int16
SAMPLE_RATE   = 16000
CHANNELS      = 1
CHUNK_SAMPLES = 1280          # ~80ms at 16kHz — OWW optimal chunk size
FORMAT        = pyaudio.paInt16

# Post-wake recording: capture up to N seconds of speech before sending
MAX_RECORD_SECONDS = 10
SILENCE_THRESHOLD  = 300      # RMS below this = silence
SILENCE_CHUNKS     = 12       # ~1 second of silence ends recording

# ── GLaDOS TTS ────────────────────────────────────────────────────────────────

try:
    sys.path.insert(0, "/opt/cofc-itip/GlaDOS")
    from glados import GLaDOS
    tts = GLaDOS()
    TTS_AVAILABLE = True
    log.info("voice.tts_ready", engine="GLaDOS")
except Exception as e:
    log.warning("voice.tts_unavailable", error=str(e))
    TTS_AVAILABLE = False
    tts = None


def speak(text: str) -> None:
    """Speak text via GLaDOS TTS. Falls back to print if unavailable."""
    log.info("voice.speak", text=text[:80])
    if TTS_AVAILABLE and tts:
        try:
            tts.speak(text)
        except Exception as e:
            log.error("voice.speak_error", error=str(e))
            print(f"[JARVIS] {text}")
    else:
        print(f"[JARVIS] {text}")


# ── Audio Helpers ─────────────────────────────────────────────────────────────

def rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


def record_utterance(stream: pyaudio.Stream) -> np.ndarray:
    """
    Record audio after wake word detection.
    Stops after SILENCE_CHUNKS consecutive silent chunks or MAX_RECORD_SECONDS.
    Returns int16 numpy array at 16kHz.
    """
    log.info("voice.recording_start")
    frames = []
    silent_streak = 0
    max_chunks = int(SAMPLE_RATE / CHUNK_SAMPLES * MAX_RECORD_SECONDS)

    for _ in range(max_chunks):
        data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
        chunk = np.frombuffer(data, dtype=np.int16)
        frames.append(chunk)
        if rms(chunk) < SILENCE_THRESHOLD:
            silent_streak += 1
            if silent_streak >= SILENCE_CHUNKS:
                break
        else:
            silent_streak = 0

    audio = np.concatenate(frames)
    log.info("voice.recording_done", duration_s=round(len(audio) / SAMPLE_RATE, 1))
    return audio


# ── STT ───────────────────────────────────────────────────────────────────────

def load_whisper() -> WhisperModel:
    log.info("voice.whisper_loading", model=WHISPER_MODEL)
    # device="cpu" — safe default; change to "cuda" if you want GPU for STT
    # For JARVIS on BB, GPU is occupied by Ollama — keep STT on CPU
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    log.info("voice.whisper_ready")
    return model


def transcribe(whisper: WhisperModel, audio: np.ndarray) -> str:
    """Transcribe int16 PCM to text using faster-whisper."""
    audio_f32 = audio.astype(np.float32) / 32768.0
    segments, _ = whisper.transcribe(audio_f32, language="en", beam_size=1)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    log.info("voice.transcribed", text=text)
    return text


# ── JARVIS Core Relay ─────────────────────────────────────────────────────────

def send_to_jarvis(utterance: str) -> str:
    """POST utterance to jarvis_core HTTP endpoint. Returns response text."""
    try:
        resp = requests.post(
            f"{JARVIS_CORE_URL}/query",
            json={"query": utterance},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        log.error("voice.jarvis_unreachable", url=JARVIS_CORE_URL)
        return "I can't reach JARVIS core right now."
    except Exception as e:
        log.error("voice.jarvis_error", error=str(e))
        return "Something went wrong talking to JARVIS."


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("voice.startup", wake_word=WAKE_WORD_MODEL, threshold=WAKE_THRESHOLD)

    # Load models
    log.info("voice.oww_loading", model=WAKE_WORD_MODEL)
    oww = WakeWordModel(
        wakeword_models=[WAKE_WORD_MODEL],
        inference_framework="tflite",
    )
    log.info("voice.oww_ready", models=list(oww.models.keys()))

    whisper = load_whisper()

    # Open audio stream
    pa = pyaudio.PyAudio()

    stream_kwargs = dict(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SAMPLES,
    )
    if AUDIO_DEVICE_INDEX is not None:
        stream_kwargs["input_device_index"] = AUDIO_DEVICE_INDEX

    stream = pa.open(**stream_kwargs)

    speak("JARVIS online. Listening.")
    log.info("voice.listening", wake_word=WAKE_WORD_MODEL)

    try:
        while True:
            # ── Wake word detection loop ───────────────────────────────────
            raw = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.int16)
            oww.predict(chunk)

            detected = False
            for name, scores in oww.prediction_buffer.items():
                score = scores[-1]
                if score > WAKE_THRESHOLD:
                    log.info("voice.wake_detected", model=name, score=round(score, 3))
                    detected = True
                    oww.reset()   # clear buffer so we don't re-trigger
                    break

            if not detected:
                continue

            # ── Wake detected — record utterance ──────────────────────────
            speak("Yeah?")
            audio = record_utterance(stream)

            if len(audio) < SAMPLE_RATE * 0.5:
                # Less than 0.5s — probably nothing useful
                log.info("voice.utterance_too_short")
                speak("I didn't catch that.")
                continue

            # ── Transcribe ────────────────────────────────────────────────
            utterance = transcribe(whisper, audio)

            if not utterance:
                speak("I didn't catch that.")
                continue

            log.info("voice.utterance", text=utterance)

            # ── Send to JARVIS core ───────────────────────────────────────
            response = send_to_jarvis(utterance)

            if response:
                speak(response)

    except KeyboardInterrupt:
        log.info("voice.shutdown", reason="KeyboardInterrupt")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        speak("JARVIS signing off.")


if __name__ == "__main__":
    run()
