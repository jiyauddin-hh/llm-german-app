# AI German Tutor ("Mira")

The live practice partner for the Henry Harvin German app. Learners ask Mira
about the lesson they are on, in **Hindi, English or Hinglish** ("Guten Tag
matlab kya hota hai?"). She explains in simple English and teaches the German
inside her answer:

> Guten Tag means "Good day"! You can say: Guten Tag, wie geht es Ihnen?

She speaks the English part in English and the German part in German, corrects
mistakes gently, and keeps to the lesson.

This project is **self-contained**: its own brain, ears and voice. It does not
use or call the AI call center or any other project.

```
German app (phone) ──wss──▶ this server :9950 ──▶ brain: local Ollama model (llama3.1:8b)
                                  │           ──▶ ears:  faster-whisper (Hindi/English/Hinglish/German)
                                  │           ──▶ voice: own XTTS server :8300, or piper
                                  └──https──▶ germanapp.100xsuite.com/api
                                              (login check, lesson content, progress)
```

## Run it

```bash
./run.sh            # the tutor → http://localhost:9950/test.html
                    # (also starts the XTTS voice server if it is not running)
./run.sh --stop     # stop the tutor (./xtts/run.sh --stop for the voice)
./run.sh --fg       # foreground (what the systemd service uses)
```

`GET /health` shows whether the brain, speech recognition and a voice are
available.

### Log page

`http://localhost:9950/logs.html` (the **Log** tab on the test page) shows,
live and in plain English, everything the tutor does: each session's login,
lesson, what the learner said or typed, what Mira heard and answered, the
German she taught, every voice part, how long each step took, and why the
session ended. Problems are orange or red. The top shows whether the brain,
voice and speech recognition are working.

It is kept in memory (last 2000 events, cleared on restart) and every event is
also a line in `.run/tutor.log`. It shows learners' names and words, so it only
opens on this PC; through the tunnel it needs `?key=<LOG_KEY>` from `.env`.

### Test page

Open `http://localhost:9950/test.html`, paste a learner's login token (the
Bearer token the app gets from `/verify-login-otp` or `/auth/google`), pick a
chapter and lesson, and start. Type, or click 🎤 to talk and click again to
send: the audio goes to the server, which transcribes it. German in Mira's
replies is highlighted.

## How a session works

1. The app opens `wss://…/ws/tutor` and sends
   `{"type":"start","token":"<Bearer token>","chapter":1,"lesson":1,"mode":"call"}`.
2. The tutor calls `GET /user/profile` **with the learner's own token**. A 401
   ends the session (`unauthorized`). The profile gives the name, level and
   motivations.
3. It loads `GET /questions/chapter/{c}/lesson/{l}` and
   `GET /user/progress/dashboard`, and turns the lesson's questions
   into a teaching brief: vocabulary, key sentences, grammar points.
4. Mira greets the learner and asks the first question. Each learner turn is
   either `{"type":"user_text","text":"..."}` or recorded speech,
   `{"type":"user_audio","audio":"<base64 wav/webm/m4a/ogg>"}`, which the
   server transcribes and echoes back as `heard`. Each answer is a `reply`
   (plus an `audio` WAV in call mode).
5. On `{"type":"end"}`, the time limit, or a disconnect, the server sends a
   `summary` (turns, XP, lesson words the learner used) and closes.

Full message reference: the docstring at the top of `tutor/session.py`.

### Messages the app receives

| type | fields | app should |
|---|---|---|
| `ready` | learner, lesson_title, mode, voice, seconds_left | show the call/chat screen |
| `heard` | text, language | show what the learner said (after `user_audio`) |
| `thinking` | — | show "Mira is thinking…" |
| `reply` | text, segments, correction, hint, difficulty | show text, highlighting `segments` with `lang: "de"`; show correction/hint like `TutorResponse` |
| `audio` | format=`wav`, engine, wav (base64), part, last | call mode: one per sentence, sent as soon as each is ready. Queue and play in order; `part` 0 starts a new reply. If none arrives, speak `reply.segments` with the app's own TTS, each in its language |
| `summary` | turns, xp, words_practised, seconds | show the result screen |
| `error` | code, message | see below |

Error codes: `unauthorized` (send to login), `busy` / `daily_limit` /
`already_active` / `unavailable` (fall back to the offline lesson),
`retry` (that turn failed or was not heard, the session continues), `time_up`,
`bad_request`.

### English + German replies

The model writes German inside «» marks. `tutor/mixed.py` splits the reply on
them into `segments`, e.g.

```json
[{"lang":"de","text":"Guten Tag"},{"lang":"en","text":" means \"Good day\"! You can say: "},
 {"lang":"de","text":"Guten Tag, wie geht es Ihnen?"}]
```

`text` is the same reply without the marks. The voice speaks each segment in
its own language and joins them into one WAV.

## Response time

Measured with the GPU free, Claude as the brain, from the moment the learner
stops talking:

| step | time |
|---|---|
| hear (whisper on the GPU) | 0.2 s |
| think (Claude Haiku) | 1.5–3 s |
| first sentence spoken (XTTS) | 0.2–2 s |
| **Mira starts speaking** | **about 2–3 s** |

The rest of the reply plays while the next sentences are made.

## Limits

Set in `.env` (see `.env.example`):

| setting | default | why |
|---|---|---|
| `MAX_SESSIONS` | 3 | the model, whisper and the voice share this one PC |
| `SESSION_MINUTES` | 15 | per session |
| `DAILY_MINUTES` | 30 | per learner per day, stored in `data/usage.json` |

One open session per learner at a time.

## Brain

Set `TUTOR_BRAIN` in `.env`:

| value | what happens |
|---|---|
| `auto` (used now) | Claude (Haiku, via the `claude` CLI). If Claude fails, that session switches to the local model and keeps the conversation so far. |
| `ollama` (code default) | Local model only (`OLLAMA_MODEL`, default `llama3.1:8b`). Free and private, but it makes teaching mistakes ("Danke is Thanks, Thank you is Vielen Dank"). |
| `claude` | Claude only. |

The local model is only fast when it fits on the GPU (RTX 3060, 12 GB).
Another project keeps `qwen2.5-coder:14b` loaded (about 10 GB) when in use;
then replies take 20 to 40 s.

## Ears (speech to text)

faster-whisper `small`, language detected per utterance (`WHISPER_LANG=auto`).
On the GPU about 0.2 s per sentence, on the CPU about 2 s; `WHISPER_DEVICE=auto`
uses the GPU when it can. The GPU needs the pip packages `nvidia-cublas-cu12`
and `nvidia-cudnn-cu12` (in requirements.txt). It is primed with the lesson's
German words and typical Hinglish questions, which is what makes "Guten Tag
matlab kya hota hai?" come out right.

## Voice

1. **XTTS-v2**, this project's own server in `xtts/` (port 8300): one natural
   voice (built-in speaker `Ana Florence`, `XTTS_SPEAKER`) that speaks both
   English and German. It runs in its own venv because Coqui TTS pins
   conflicting dependencies. Setup, once:

   ```bash
   python3 -m venv xtts/venv
   xtts/venv/bin/pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
   xtts/venv/bin/pip install coqui-tts==0.27.5 transformers==4.57.6 fastapi "uvicorn[standard]"
   ./xtts/run.sh        # first start downloads the model (~1.8 GB) to ~/.local/share/tts
   ```

   It needs about 2 GB of free GPU memory; without it, it runs on the CPU and
   is much slower (about 14 s a reply). It handles one sentence at a time,
   cuts the babble XTTS adds after very short phrases, and restarts itself if
   the GPU gets into a bad state.
2. **Piper**, the local fallback (in `voices/`): `de_DE-thorsten-medium` for
   German and `en_US-lessac-medium` for English. Robotic but fast.
3. **Neither available**: the reply is sent as text only and the app speaks it.

## XP

Not sent to the backend. `POST /user/progress/lesson` marks a lesson
**completed**, which a conversation must not do. XP is written to
`data/xp_log.jsonl` (2 per learner turn plus 1 per lesson word used, max 60)
until the backend adds a tutor endpoint, e.g. `POST /user/progress/tutor`.

## Going public (for the Play Store app)

Phones need a fixed `wss://` address. Use a named Cloudflare tunnel on your
domain:

```bash
cloudflared tunnel login                              # once, picks the 100xsuite.com zone
cloudflared tunnel create german-tutor                # prints <TUNNEL_ID>
cloudflared tunnel route dns german-tutor tutor.100xsuite.com
# put <TUNNEL_ID> into deploy/cloudflared-tutor.yml, then:
cloudflared tunnel --config deploy/cloudflared-tutor.yml run
```

The app then connects to `wss://tutor.100xsuite.com/ws/tutor`.

Start everything automatically on boot with `deploy/german-tutor.service`
(install steps are inside the file). Also turn off sleep/suspend on this PC.

## Files

```
main.py               FastAPI app: /ws/tutor, /health, static test page
tutor/session.py      the WebSocket conversation (protocol docs at the top)
tutor/german_api.py   German backend client (profile = login check, lesson, dashboard)
tutor/lesson.py       lesson questions → teaching brief
tutor/prompt.py       Mira's instructions, per learner and lesson
tutor/brain.py        Ollama (default) / Claude CLI brains
tutor/stt.py          speech to text (faster-whisper)
tutor/mixed.py        splits English + German replies into segments
tutor/tts.py          own XTTS → piper → text-only
tutor/limits.py       session/day limits, usage and XP files
tutor/events.py       the live event log behind the Log page
xtts/                 the project's own XTTS voice server (own venv)
static/test.html      browser test page
static/logs.html      live log page
deploy/               systemd service + Cloudflare tunnel config
```
