"""
Speech for call mode. A reply is English with German phrases in it, so each
part (see mixed.py) is spoken in its own language and the parts are joined into
one WAV.

1. XTTS-v2 — this project's own voice server (xtts/, port 8300). One natural
   voice speaks both English and German, so the switch between languages
   sounds like one person.
2. Piper — local fallback when the worker is down or busy: de_DE-thorsten for
   German, en_US-lessac for English. Robotic but fast, and runs on CPU.
3. Neither — the reply goes out as text only, and the app speaks it itself.

Returns a complete WAV file (bytes), which the app can play directly.
sentences() cuts a reply into sentence-sized pieces, so the first one can be
sent (and heard) while the rest are still being made.
"""

import asyncio
import io
import json
import logging
import re
import urllib.error
import urllib.request
import wave

from . import config, events

log = logging.getLogger("tutor")

_piper: dict = {}

# A short breath between an English part and a German part.
GAP_SECONDS = 0.15


def xtts_available() -> bool:
    try:
        with urllib.request.urlopen(f"{config.XTTS_URL}/health", timeout=2) as r:
            return json.load(r).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _piper_file(lang: str):
    name = config.PIPER_VOICE_EN if lang == "en" else config.PIPER_VOICE_DE
    path = config.VOICES_DIR / f"{name}.onnx"
    return path if path.exists() else None


def piper_available() -> bool:
    return _piper_file("de") is not None


def engine() -> str:
    if xtts_available():
        return "xtts"
    if piper_available():
        return "piper"
    return "none"


def _xtts_sync(text: str, lang: str) -> bytes:
    req = urllib.request.Request(
        f"{config.XTTS_URL}/synth", data=json.dumps({"text": text, "language": lang}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()  # audio/wav; errors arrive as HTTPError


def _piper_voice(lang: str):
    # No English voice installed: the German one reads it, better than silence.
    path = _piper_file(lang) or _piper_file("de")
    if path not in _piper:
        from piper import PiperVoice

        _piper[path] = PiperVoice.load(str(path))
    return _piper[path]


def _piper_sync(text: str, lang: str) -> bytes:
    voice = _piper_voice(lang)
    chunks = list(voice.synthesize(text))
    if not chunks:
        return b""
    return _wav(b"".join(c.audio_int16_bytes for c in chunks), voice.config.sample_rate)


def _wav(frames: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def _join(wavs: list[bytes]) -> bytes:
    """One WAV from several, with a short gap between them. Parts in a
    different format from the first one are dropped rather than played wrong."""
    frames, params = [], None
    for data in wavs:
        if not data:
            continue
        with wave.open(io.BytesIO(data)) as w:
            p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = p
            elif p != params:
                log.warning("dropping a voice part in a different format: %s vs %s", p, params)
                continue
            if frames:
                frames.append(b"\0" * int(GAP_SECONDS * p[2]) * p[0] * p[1])
            frames.append(w.readframes(w.getnframes()))
    if params is None:
        return b""
    return _wav(b"".join(frames), params[2], params[0], params[1])


def duration(data: bytes) -> float:
    """Seconds of audio in a WAV."""
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def _speak_sync(parts: list[tuple[str, str]]) -> tuple[bytes, str]:
    if xtts_available():
        try:
            return _join([_xtts_sync(text, lang) for lang, text in parts]), "xtts"
        except Exception as e:
            events.add("The XTTS voice failed; using the piper voice instead.", step="voice",
                       level="warning", reason=str(e)[:200])
    if piper_available():
        try:
            return _join([_piper_sync(text, lang) for lang, text in parts]), "piper"
        except Exception as e:
            events.add("The piper voice failed too; sending text only.", step="voice",
                       level="error", reason=str(e)[:200])
    return b"", "none"


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def sentences(parts: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """
    [(lang, text), ...] cut at sentence ends into pieces to speak one by one.
    A piece can still mix languages: "Guten Tag means good day."
    """
    pieces: list[list[tuple[str, str]]] = [[]]
    for lang, text in parts:
        bits = _SENTENCE_END.split(text)
        for i, bit in enumerate(bits):
            if bit.strip():
                pieces[-1].append((lang, bit.strip()))
            ended = i < len(bits) - 1 or bit.rstrip().endswith((".", "!", "?"))
            if ended and pieces[-1]:
                pieces.append([])
    return [p for p in pieces if p]


async def speak(parts: list[tuple[str, str]]) -> tuple[bytes, str]:
    """
    (wav bytes, engine used) for [(lang, text), ...] from mixed.speakable().
    Empty bytes means: let the app speak it.
    """
    if not parts:
        return b"", "none"
    return await asyncio.to_thread(_speak_sync, parts)
