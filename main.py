"""
AI German Tutor — the live practice partner ("Mira") for the Henry Harvin
German app.

    ./run.sh                 start on http://127.0.0.1:9950
    /ws/tutor                the app's WebSocket (protocol: tutor/session.py)
    /health                  liveness + what is available
    /test.html               browser test page
    /logs.html               live log of what every session is doing
    /api/logs                the events behind it (this PC only, or ?key=LOG_KEY)

Self-contained: its own brain (local Ollama model), its own speech recognition
(faster-whisper) and its own voice server (xtts/, port 8300).
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles

from tutor import brain, config, events, german_api, limits, session, stt, tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    events.add("The tutor server is starting.")
    if not config.GERMAN_API_KEY:
        events.add("GERMAN_API_KEY is not set in .env, so every login check will fail.", level="error")
    if config.DAILY_MINUTES <= 0:
        events.add(f"DAILY_MINUTES is {config.DAILY_MINUTES} in .env, so every learner is refused "
                   "with daily_limit. Set it to a number of minutes.", level="error")
    # Load whisper before the first learner speaks (~2 s), off the event loop.
    await asyncio.to_thread(stt.warm_up)
    b = await asyncio.to_thread(brain.status)
    voice = await asyncio.to_thread(tts.engine)
    events.add("Ready. " + ", ".join([
        f"brain: {b['mode']}"
        + (f" (Claude {b['claude']['model']})" if b["mode"] != "ollama" and b["claude"]["available"] else "")
        + (f" (local {b['ollama']['model']})" if b["mode"] == "ollama" else ""),
        f"voice: {voice}",
        f"speech recognition: whisper {config.WHISPER_MODEL} on {stt.device}",
        f"limits: {config.MAX_SESSIONS} sessions at once, {config.SESSION_MINUTES} min each, "
        f"{config.DAILY_MINUTES} min a day per learner",
    ]) + ".", level="ok")
    if voice != "xtts":
        events.add("The XTTS voice server is not running, so Mira uses the robotic piper voice. "
                   "Start it with ./xtts/run.sh.", level="warning")
    yield
    events.add("The tutor server is stopping.")
    await german_api.close()
    await brain.shutdown()


app = FastAPI(title="AI German Tutor", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "brain": brain.status(),
        "voice": tts.engine(),
        "speech_to_text": {"available": stt.available(), "model": config.WHISPER_MODEL,
                           "device": stt.device},
        "active_sessions": limits.active_sessions(),
        "max_sessions": config.MAX_SESSIONS,
    }


def _log_access(request: Request):
    """
    The log shows learners' names and words. It is open on this PC; through
    the Cloudflare tunnel (which adds these headers) it needs ?key=LOG_KEY.
    """
    if not any(h in request.headers for h in ("cf-connecting-ip", "x-forwarded-for", "cf-ray")):
        return
    key = request.query_params.get("key", "")
    if not (config.LOG_KEY and hmac.compare_digest(key, config.LOG_KEY)):
        raise HTTPException(403, "The log is only available on the tutor's own PC.")


@app.get("/api/logs")
def logs(request: Request, after: int = 0):
    _log_access(request)
    return {"events": events.after(after), "last_id": events.last_id(),
            "max_events": events.MAX_EVENTS}


@app.websocket("/ws/tutor")
async def ws_tutor(ws: WebSocket):
    await session.run(ws)


app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
