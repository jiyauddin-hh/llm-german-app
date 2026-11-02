"""
/ws/tutor — one learner's practice session.

Protocol (JSON text frames):

  app -> server
    {"type": "start", "token": "<Sanctum token>", "chapter": 1, "lesson": 1,
     "mode": "call" | "chat"}                       must be the first message
    {"type": "user_text", "text": "..."}            each learner turn: English,
                                                    Hindi, Hinglish or German
    {"type": "user_audio", "audio": "<base64>"}     or the turn as recorded
                                                    speech (wav, webm, m4a,
                                                    ogg; max 30 s); the server
                                                    transcribes it
    {"type": "end"}                                 learner hung up / left

  server -> app
    {"type": "ready", "learner": "Anna", "lesson_title": "...", "mode": "call",
     "voice": "xtts" | "piper" | "none", "brain": "claude" | "ollama",
     "seconds_left": 900}
    {"type": "heard", "text": "...", "language": "hi"}
                                                    what the server understood
                                                    from user_audio
    {"type": "thinking"}                            reply is being prepared
    {"type": "reply", "text": "...", "correction": str|null, "hint": str|null,
     "difficulty": "easier" | "same" | "harder",
     "segments": [{"lang": "en" | "de", "text": "..."}, ...]}
                                                    text is English with German
                                                    in it; segments is the same
                                                    text split by language, so
                                                    the app can highlight the
                                                    German (joined, they equal
                                                    text)
    {"type": "audio", "format": "wav", "engine": "xtts", "wav": "<base64>",
     "part": 0, "last": false}                      call mode only, after reply:
                                                    one per sentence, sent as
                                                    soon as each is ready, so
                                                    the app queues and plays
                                                    them in order
    {"type": "summary", "turns": 6, "xp": 18, "words_practised": [...],
     "seconds": 312}                                last message before close
    {"type": "error", "code": "...", "message": "..."}

  error codes: bad_request, unauthorized, unavailable, busy, daily_limit,
               already_active, retry (this turn failed, session continues),
               time_up
"""

import asyncio
import base64
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from . import brain, config, events, german_api, lesson, limits, mixed, prompt, stt, tts

log = logging.getLogger("tutor")

START_TIMEOUT = 15
# base64 of MAX_AUDIO_SECONDS of uncompressed 48 kHz stereo, with room to spare.
MAX_AUDIO_B64 = 8 * 1024 * 1024


class _Closed(Exception):
    pass


async def _send(ws: WebSocket, payload: dict):
    try:
        await ws.send_json(payload)
    except (WebSocketDisconnect, RuntimeError) as e:
        raise _Closed() from e


async def _error(ws: WebSocket, code: str, message: str):
    try:
        await ws.send_json({"type": "error", "code": code, "message": message})
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _receive(ws: WebSocket, timeout: float) -> dict:
    raw = await asyncio.wait_for(ws.receive_text(), timeout)
    try:
        msg = json.loads(raw)
    except ValueError:
        return {"type": "_invalid"}
    return msg if isinstance(msg, dict) else {"type": "_invalid"}


def _int(value, lo=1, hi=999):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _completed(dash: dict) -> list[str]:
    out = []
    for ch in dash.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        for ls in ch.get("lessons") or []:
            if isinstance(ls, dict) and ls.get("is_completed"):
                out.append(f"{ch.get('chapter_number')}.{ls.get('lesson_number')}")
    return out


def _words_used(texts: list[str], words: set[str]) -> list[str]:
    said = set()
    for t in texts:
        for w in t.lower().replace(",", " ").replace(".", " ").replace("?", " ").replace("!", " ").split():
            said.add(w.strip("'\"()"))
    return sorted(w for w in words if w in said)


async def run(ws: WebSocket):
    await ws.accept()
    ev = events.SessionLog()
    ev("connect", "A learner's app connected. Waiting for its start message.")

    # --- 1. start message -------------------------------------------------
    try:
        start = await _receive(ws, START_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        ev("connect", f"No start message within {START_TIMEOUT}s. Closed.", "warning")
        await _error(ws, "bad_request", "Expected a start message.")
        await _safe_close(ws)
        return

    token = start.get("token")
    chapter = _int(start.get("chapter"))
    lesson_no = _int(start.get("lesson"))
    mode = start.get("mode", "call")
    if (start.get("type") != "start" or not isinstance(token, str) or not token
            or len(token) > 500 or chapter is None or lesson_no is None
            or mode not in ("call", "chat")):
        ev("connect", "The start message was invalid (missing token, chapter, lesson or mode). Closed.",
           "error", received_type=str(start.get("type"))[:30])
        await _error(ws, "bad_request",
                     "First message must be {type:'start', token, chapter, lesson, mode:'call'|'chat'}.")
        await _safe_close(ws)
        return
    ev("connect", f"Start: chapter {chapter}, lesson {lesson_no}, {mode} mode "
                  f"({'voice call' if mode == 'call' else 'text chat'}).")

    # --- 2. who is this? (the backend decides) ----------------------------
    t = time.monotonic()
    try:
        user = await german_api.profile(token)
    except german_api.Unauthorized:
        ev("login", "Login check failed: the German app's server rejected the token "
                    "(expired or wrong). The learner must log in again.", "error")
        await _error(ws, "unauthorized", "Your login has expired. Please log in again.")
        await _safe_close(ws)
        return
    except german_api.BackendError as e:
        ev("login", "Could not reach the German app's server to check the login.", "error", reason=str(e))
        await _error(ws, "unavailable", "Mira can't reach the course server right now.")
        await _safe_close(ws)
        return

    user_id = int(user["id"])
    first_name = ((user.get("name") or "").split() or [""])[0][:40]
    ev.learner = first_name or f"user {user_id}"
    level = (user.get("profile") or {}).get("level")
    ev("login", f"Login OK: {first_name or 'the learner'} (user {user_id}), level {level or 'unknown'}.",
       "ok", took=_secs(t))

    # --- 3. capacity ------------------------------------------------------
    try:
        await limits.acquire(user_id)
    except limits.LimitReached as e:
        ev("limits", f"Refused ({e.code}): {e.message}", "warning",
           active_sessions=limits.active_sessions(), max_sessions=config.MAX_SESSIONS,
           used_today_min=round(limits.seconds_used_today(user_id) / 60, 1),
           daily_limit_min=config.DAILY_MINUTES)
        await _error(ws, e.code, e.message)
        await _safe_close(ws)
        return
    ev("limits", f"Session slot taken ({limits.active_sessions()} of {config.MAX_SESSIONS} in use).",
       used_today_min=round(limits.seconds_used_today(user_id) / 60, 1),
       daily_limit_min=config.DAILY_MINUTES)

    started = time.monotonic()
    the_brain = None
    learner_texts: list[str] = []
    brief = None
    reason = "the app disconnected"
    try:
        # --- 4. lesson + progress, in parallel ------------------------------
        t = time.monotonic()
        try:
            data, dash = await asyncio.gather(
                german_api.lesson(token, chapter, lesson_no),
                german_api.dashboard(token),
            )
        except german_api.Unauthorized:
            ev("lesson", "The login expired while loading the lesson.", "error")
            await _error(ws, "unauthorized", "Your login has expired. Please log in again.")
            reason = "the login expired"
            return
        except german_api.BackendError as e:
            ev("lesson", "Could not load the lesson; Mira will do a general beginner conversation.",
               "warning", reason=str(e))
            data, dash = {}, {}
        brief = lesson.build(data or {}, chapter, lesson_no)
        if brief.found:
            ev("lesson", f"Lesson loaded: \"{brief.title or 'untitled'}\" — {_n(brief.vocabulary, 'word')}, "
                         f"{_n(brief.sentences, 'sentence')}, {_n(brief.grammar, 'grammar point')}.",
               "ok", took=_secs(t), vocabulary=brief.vocabulary[:10])
        else:
            ev("lesson", f"No material found for chapter {chapter}, lesson {lesson_no}; "
                         "Mira will do a general beginner conversation.", "warning", took=_secs(t))

        the_brain = brain.make(prompt.system_prompt(user, brief, mode, _completed(dash)))
        budget = min(config.SESSION_MINUTES * 60, limits.remaining_seconds(user_id))
        deadline = started + budget
        voice = tts.engine() if mode == "call" else "none"

        await _send(ws, {
            "type": "ready",
            "learner": first_name,
            "lesson_title": brief.title,
            "mode": mode,
            "voice": voice,
            "brain": the_brain.name,
            "seconds_left": int(budget),
        })
        ev("brain", f"Session ready. Brain: {the_brain.name}. Voice: {voice}. "
                    f"Time allowed: {int(budget // 60)} min {int(budget % 60)} s.", "ok")

        # --- 5. Mira speaks first -------------------------------------------
        ev("think", "Mira is preparing her greeting.")
        await _turn(ws, the_brain, prompt.KICKOFF, mode, ev)

        # --- 6. conversation loop -------------------------------------------
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                ev("end", "Time is up for this session.", "warning")
                await _error(ws, "time_up", "That's all the time for this session. Toll gemacht!")
                reason = "time ran out"
                break
            try:
                msg = await _receive(ws, left)
            except asyncio.TimeoutError:
                continue  # loop re-checks the deadline and sends time_up
            kind = msg.get("type")
            if kind == "end":
                reason = "the learner ended it"
                break
            if kind == "user_audio":
                msg = {"text": await _hear(ws, msg.get("audio"), sorted(brief.words), ev)}
            elif kind == "user_text":
                if isinstance(msg.get("text"), str) and msg["text"].strip():
                    ev("hear", f"Learner typed: \"{msg['text'].strip()[:300]}\"")
            else:
                ev("connect", f"Unknown message type {str(kind)[:30]!r} ignored.", "warning")
                await _error(ws, "bad_request", f"Unknown message type: {str(kind)[:30]!r}")
                continue
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                continue  # speech recognition heard nothing
            text = " ".join(text.split())[: config.MAX_TEXT_CHARS]
            learner_texts.append(text)
            await _turn(ws, the_brain, text, mode, ev)

    except (_Closed, WebSocketDisconnect):
        pass
    except Exception as e:
        log.exception("session crashed user=%s", user_id)
        ev("error", "The session crashed (a bug on the server).", "error",
           reason=f"{type(e).__name__}: {e}")
        reason = "a server error"
        await _error(ws, "unavailable", "Something went wrong on Mira's side.")
    finally:
        seconds = time.monotonic() - started
        words = _words_used(learner_texts, brief.words) if brief else []
        turns = len(learner_texts)
        xp = min(60, 2 * turns + len(words)) if turns else 0
        summary = {"type": "summary", "turns": turns, "xp": xp,
                   "words_practised": words, "seconds": int(seconds)}
        # Usage and the slot are settled before the summary goes out: the app
        # may open its next session the moment it reads the summary, and must
        # not be told "already open" by this one.
        limits.add_usage(user_id, seconds)
        await limits.release(user_id)
        try:
            await ws.send_json(summary)
        except (WebSocketDisconnect, RuntimeError):
            pass
        limits.log_xp({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "user_id": user_id,
            "chapter": chapter, "lesson": lesson_no, "mode": mode,
            "brain": the_brain.name if the_brain else None, **{
                k: summary[k] for k in ("turns", "xp", "words_practised", "seconds")},
        })
        ev("end", f"Session ended because {reason}. {_n(turns, 'learner turn')}, {xp} XP, "
                  f"{int(seconds // 60)} min {int(seconds % 60)} s.", "ok",
           words_practised=words or None)
        if the_brain:
            await the_brain.close()
        await _safe_close(ws)


def _n(count, noun: str) -> str:
    """'1 word', '3 words' (count may be a number or a list)."""
    n = count if isinstance(count, int) else len(count)
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _secs(since: float) -> str:
    return f"{time.monotonic() - since:.2f} s"


async def _hear(ws: WebSocket, audio, words: list[str], ev) -> str:
    """The learner's speech as text, or '' (after telling the app) if none."""
    if not isinstance(audio, str) or not audio or len(audio) > MAX_AUDIO_B64:
        ev("hear", "Received audio that was empty or too large; ignored.", "warning")
        await _error(ws, "bad_request", "user_audio needs base64 audio, at most 30 seconds.")
        return ""
    try:
        data = base64.b64decode(audio, validate=True)
    except ValueError:
        ev("hear", "Received audio that was not valid base64; ignored.", "warning")
        await _error(ws, "bad_request", "user_audio is not valid base64.")
        return ""
    ev("hear", f"Learner spoke: received {len(data) // 1024} KB of audio. Transcribing.")
    await _send(ws, {"type": "thinking"})
    t = time.monotonic()
    try:
        text, lang = await stt.transcribe(data, words)
    except stt.STTError as e:
        ev("hear", "Could not understand the audio; asked the learner to repeat.", "warning",
           reason=str(e), took=_secs(t))
        await _error(ws, "retry", "Mira couldn't hear that. Please say it again.")
        return ""
    if not text:
        ev("hear", "The audio had no speech in it; asked the learner to repeat.", "warning", took=_secs(t))
        await _error(ws, "retry", "Mira didn't hear anything. Please say it again.")
        return ""
    ev("hear", f"Heard: \"{text}\"", "ok", language=lang, took=_secs(t), on=stt.device)
    await _send(ws, {"type": "heard", "text": text, "language": lang})
    return text


async def _turn(ws: WebSocket, the_brain, text: str, mode: str, ev):
    await _send(ws, {"type": "thinking"})
    before = the_brain.name
    t = time.monotonic()
    try:
        r = await the_brain.ask(text)
    except brain.BrainError as e:
        ev("think", f"The brain ({the_brain.name}) failed; asked the learner to say it again.", "error",
           reason=str(e), took=_secs(t))
        await _send(ws, {"type": "error", "code": "retry",
                         "message": "Mira didn't catch that. Please say it again."})
        return
    if the_brain.name != before:
        ev("brain", f"{before} failed, so this session switched to {the_brain.name} for the rest of it.",
           "warning")
    segs = mixed.segments(r.reply)
    reply_text = "".join(x["text"] for x in segs)
    german = [x["text"].strip() for x in segs if x["lang"] == "de"]
    ev("reply", f"Mira: \"{reply_text}\"", "ok", took=_secs(t), brain=the_brain.name,
       german=german or None, correction=mixed.plain(r.correction), hint=mixed.plain(r.hint),
       difficulty=r.difficulty if r.difficulty != "same" else None)
    await _send(ws, {"type": "reply", "text": reply_text,
                     "correction": mixed.plain(r.correction), "hint": mixed.plain(r.hint),
                     "difficulty": r.difficulty, "segments": segs})
    if mode == "call":
        # Sentence by sentence: the learner hears the first one while the
        # rest are made, instead of waiting for the whole reply.
        pieces = tts.sentences(mixed.speakable(segs))
        for i, piece in enumerate(pieces):
            t = time.monotonic()
            wav, used = await tts.speak(piece)
            said = " ".join(f"[{lang}] {text}" for lang, text in piece)
            if wav:
                ev("voice", f"Spoke part {i + 1} of {len(pieces)} with {used}: {said}",
                   "warning" if used != "xtts" else "info",
                   took=_secs(t), length=f"{tts.duration(wav):.1f} s")
                await _send(ws, {"type": "audio", "format": "wav", "engine": used,
                                 "wav": base64.b64encode(wav).decode(),
                                 "part": i, "last": i == len(pieces) - 1})
            else:
                ev("voice", f"No voice available for part {i + 1}; the app speaks the text itself.",
                   "warning")


async def _safe_close(ws: WebSocket):
    try:
        await ws.close()
    except (RuntimeError, WebSocketDisconnect):
        pass
