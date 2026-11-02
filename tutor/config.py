"""
Settings, all from the environment (.env next to main.py).

Nothing here is secret except GERMAN_API_KEY, and that key is already shipped
inside the mobile app — it identifies the client, it does not authenticate the
learner. The learner's own Bearer token does that, and it is never stored.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- German app backend (Laravel) ------------------------------------------
GERMAN_API_BASE = os.getenv("GERMAN_API_BASE", "https://germanapp.100xsuite.com/api").rstrip("/")
GERMAN_API_KEY = os.getenv("GERMAN_API_KEY", "")
# Profile lookups double as the login check, so they are cached briefly: a
# conversation should not cost the backend one request per sentence.
PROFILE_CACHE_SECONDS = int(os.getenv("PROFILE_CACHE_SECONDS", "300"))

# --- Brain -------------------------------------------------------------------
#   ollama  a local model through Ollama — free, private, works offline (default)
#   claude  the claude CLI, with the Claude Code login on this machine
#   auto    claude, and the local model whenever claude fails or is missing
BRAIN = os.getenv("TUTOR_BRAIN", "ollama").lower()

MODEL = os.getenv("TUTOR_MODEL", "claude-haiku-4-5")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
# llama3.1:8b speaks the best German of the models installed here.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
# How long Ollama keeps the model on the GPU after the last turn. The GPU is
# shared with other projects, so it is not pinned forever.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
# A learner waiting on a reply hears silence, but nothing hangs up on them the
# way a phone line does, so this is loose.
BRAIN_TIMEOUT = int(os.getenv("BRAIN_TIMEOUT", "25"))
CLAUDE_CLI = os.getenv("CLAUDE_CLI", "")

# --- Voice -----------------------------------------------------------------
# This project's own XTTS voice server (xtts/run.sh). Piper is the fallback.
XTTS_URL = os.getenv("XTTS_URL", "http://127.0.0.1:8300")
PIPER_VOICE_DE = os.getenv("PIPER_VOICE_DE", "de_DE-thorsten-medium")
PIPER_VOICE_EN = os.getenv("PIPER_VOICE_EN", "en_US-lessac-medium")
VOICES_DIR = ROOT / "voices"

# --- Speech to text --------------------------------------------------------
# faster-whisper. "small" is the smallest model that copes with Hinglish;
# "base" is faster and worse. auto: the GPU when it can load there, else CPU.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
# auto: detect per utterance (handles Hindi, English, Hinglish and German).
WHISPER_LANG = os.getenv("WHISPER_LANG", "auto")
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "30"))

# --- Limits ----------------------------------------------------------------
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "3"))
SESSION_MINUTES = int(os.getenv("SESSION_MINUTES", "15"))
DAILY_MINUTES = int(os.getenv("DAILY_MINUTES", "30"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "500"))

# --- Log page ----------------------------------------------------------------
# /logs.html shows learners' names and words. It always works on this PC; from
# outside (through the Cloudflare tunnel) only with ?key=<LOG_KEY>, and not at
# all while LOG_KEY is empty.
LOG_KEY = os.getenv("LOG_KEY", "")

# --- Storage ---------------------------------------------------------------
DATA_DIR = Path(os.getenv("TUTOR_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
