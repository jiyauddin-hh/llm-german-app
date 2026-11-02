"""
The German tutor's own XTTS-v2 voice server: a natural voice that speaks both
English and German, so Mira sounds like one person when she switches between
an explanation and a German phrase.

Runs in its own venv (xtts/venv), because Coqui TTS pins dependencies that
conflict with the tutor's. Nothing here belongs to or talks to any other
project. It uses one of XTTS's built-in speakers, so no voice clips are needed.

    ./xtts/run.sh           listens on http://127.0.0.1:8300

    GET  /health            {"status": "ok" | "loading", "device", "speaker"}
    POST /synth             {"text": "...", "language": "en" | "de"}
                            -> audio/wav
"""

import io
import os
import threading
import wave

import numpy as np
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

MODEL = os.getenv("XTTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2")
DEVICE = os.getenv("XTTS_DEVICE", "cuda")
SPEAKER = os.getenv("XTTS_SPEAKER", "Ana Florence")
LANGUAGES = {"en", "de"}

# XTTS babbles after very short inputs: "Guten Tag" comes back as 6-8 s of
# audio, the real words first and invented ones after. Speech runs at about
# 12-15 characters a second, so anything past this is cut.
def _max_seconds(text: str) -> float:
    return 0.8 + len(text) / 8

PARAMS = {
    "temperature": 0.75,
    "repetition_penalty": 5.0,
    "top_k": 50,
    "top_p": 0.85,
    "speed": 0.95,  # a little slower: learners are listening for every sound
}

app = FastAPI(title="German tutor XTTS")
_tts = None
_device = None
# The model is not safe to use from two threads at once: on the GPU, two
# overlapping requests trigger a CUDA assert that breaks every later request.
_lock = threading.Lock()


@app.on_event("startup")
def _load():
    global _tts, _device
    from TTS.api import TTS

    print(f"Loading XTTS-v2 on {DEVICE} (first run downloads ~1.8GB) ...", flush=True)
    try:
        _tts, _device = TTS(MODEL).to(DEVICE), DEVICE
    except Exception as e:
        # A full GPU is the usual reason. CPU works, several times slower.
        print(f"GPU load failed ({e}); using the CPU instead.", flush=True)
        _tts, _device = TTS(MODEL).to("cpu"), "cpu"
    # The first synthesis on a fresh model is several times slower; pay it now.
    _tts.tts(text="Hallo.", language="de", speaker=SPEAKER, **PARAMS)
    print(f"XTTS-v2 ready on {_device}, speaker {SPEAKER!r}", flush=True)


@app.get("/health")
def health():
    return {"status": "ok" if _tts is not None else "loading", "device": _device, "speaker": SPEAKER}


@app.post("/synth")
def synth(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    lang = payload.get("language") or "de"
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    if lang not in LANGUAGES:
        return JSONResponse({"error": f"language must be one of {sorted(LANGUAGES)}"}, status_code=400)
    if _tts is None:
        return JSONResponse({"error": "model still loading"}, status_code=503)
    try:
        with _lock:
            samples = _tts.tts(text=text, language=lang, speaker=SPEAKER, **PARAMS)
    except Exception as e:
        if "CUDA error" in str(e):
            # The GPU context is now unusable for good. Exit, so xtts/run.sh
            # starts a fresh process; the tutor uses piper meanwhile.
            print(f"CUDA error, restarting: {e}", flush=True)
            os._exit(1)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

    rate = _tts.synthesizer.output_sample_rate
    audio = np.asarray(samples, dtype=np.float32)[: int(_max_seconds(text) * rate)]
    # Fade the last 50 ms, so a cut never ends in a click.
    fade = min(len(audio), int(0.05 * rate))
    if fade:
        audio[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return Response(buf.getvalue(), media_type="audio/wav")
