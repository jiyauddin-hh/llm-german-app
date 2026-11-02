"""
The tutor's brain. Two engines, chosen by TUTOR_BRAIN (see config.py):

ClaudeBrain — one long-lived `claude` CLI process per session (optional, off
  by default): a persistent stream-json process keeps the conversation in the
  model's context, and uses this machine's Claude Code login, so there is no
  API key. No tools and no MCP
  servers: a learner must not be able to make the tutor read files, run
  commands or reach the CRM.

OllamaBrain — a local model through Ollama's HTTP API. Free and private, and
  keeps working when the internet or the Claude login does not. It holds the
  conversation history itself, because Ollama's chat API is stateless.

AutoBrain — Claude first; on the first Claude failure the session moves to the
  local model for good, carrying the conversation so far with it.
"""

import asyncio
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import config

log = logging.getLogger("tutor")

_NPM_GLOBAL = Path.home() / ".npm-global" / "bin" / "claude"
_workdir = None


def cli_path():
    if config.CLAUDE_CLI:
        return config.CLAUDE_CLI if Path(config.CLAUDE_CLI).exists() else None
    return shutil.which("claude") or (str(_NPM_GLOBAL) if _NPM_GLOBAL.exists() else None)


def available() -> bool:
    return cli_path() is not None


class BrainError(Exception):
    """The model could not produce a reply."""


def _empty_workdir() -> str:
    # The CLI reads CLAUDE.md and nearby files from its cwd; give it nothing.
    global _workdir
    if _workdir is None:
        _workdir = tempfile.mkdtemp(prefix="german-tutor-")
    return _workdir


def _argv(system: str):
    return [
        cli_path(), "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--system-prompt", system,
        "--model", config.MODEL,
        "--disallowed-tools", "Bash", "Read", "Write", "Edit", "Glob", "Grep",
        "WebFetch", "WebSearch", "Task", "TodoWrite", "NotebookEdit",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        # Extended thinking is on by default and costs 15-40s before every
        # reply — measured. A tutor turn needs ~2s, so it is switched off.
        "--settings", '{"alwaysThinkingEnabled": false}',
    ]


@dataclass
class TutorReply:
    reply: str
    correction: str | None = None
    hint: str | None = None
    difficulty: str = "same"


def _opt(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and value.lower() not in ("null", "none") else None


def parse(raw: str) -> TutorReply:
    """
    The model's JSON reply, defensively. Same rules as the app's
    TutorResponse.parse: strip code fences, take the outermost {...}, and fall
    back to the raw text as the reply rather than fail the turn.
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            d = json.loads(text[start:end + 1])
            reply = _opt(d.get("reply"))
            if reply:
                diff = d.get("suggestedDifficulty")
                return TutorReply(
                    reply=reply,
                    correction=_opt(d.get("correctionNote")),
                    hint=_opt(d.get("hint")),
                    difficulty=diff if diff in ("easier", "same", "harder") else "same",
                )
        except (ValueError, AttributeError):
            pass
    prose = re.sub(r"```(?:json)?", "", text).strip()
    if not prose:
        raise BrainError("The model returned no text.")
    return TutorReply(reply=prose)


class ClaudeBrain:
    """One session's claude process. Never shared between learners."""

    name = "claude"

    def __init__(self, system: str):
        self.system = system
        self._proc = None
        self._lock = asyncio.Lock()

    async def _start(self):
        if self._proc and self._proc.returncode is None:
            return
        if not available():
            raise BrainError("The claude CLI is not installed on the server.")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *_argv(self.system),
                cwd=_empty_workdir(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 ** 20,
            )
        except OSError as e:
            raise BrainError(f"Could not start the claude CLI: {e}") from e

    async def ask(self, text: str) -> TutorReply:
        async with self._lock:
            await self._start()
            line = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            })
            try:
                self._proc.stdin.write((line + "\n").encode())
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                self._proc = None
                raise BrainError("The model process stopped.") from e
            try:
                raw = await asyncio.wait_for(self._read_result(), config.BRAIN_TIMEOUT)
            except asyncio.TimeoutError as e:
                # The process may still answer this turn later and desync the
                # next one, so start fresh next time.
                await self.close()
                raise BrainError(f"The model took longer than {config.BRAIN_TIMEOUT}s.") from e
            return parse(raw)

    async def _read_result(self) -> str:
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                self._proc = None
                raise BrainError("The model process closed unexpectedly.")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") != "result":
                continue
            if msg.get("is_error"):
                raise BrainError(msg.get("result") or "The model reported an error.")
            return (msg.get("result") or "").strip()

    async def close(self):
        proc, self._proc = self._proc, None
        if not proc or proc.returncode is not None:
            return
        try:
            proc.stdin.close()
        except (BrokenPipeError, RuntimeError):
            pass
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


# --- Local model (Ollama) ----------------------------------------------------

_ollama_http: httpx.AsyncClient | None = None

# Ollama's chat API is stateless, so the whole conversation is sent each turn.
# Keep it bounded: a 15-minute lesson is ~30 turns, and the lesson brief lives
# in the system prompt anyway.
MAX_HISTORY_MESSAGES = 30

# Loading a model onto the GPU takes several seconds the first time, on top of
# the reply itself, so the local model gets a longer leash than claude.
OLLAMA_TIMEOUT = 90


def _ollama() -> httpx.AsyncClient:
    global _ollama_http
    if _ollama_http is None:
        _ollama_http = httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=OLLAMA_TIMEOUT)
    return _ollama_http


def ollama_available() -> bool:
    """True when Ollama answers and has the configured model installed."""
    try:
        r = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        names = {m.get("name") for m in r.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return False
    want = config.OLLAMA_MODEL
    return want in names or f"{want}:latest" in names


class OllamaBrain:
    """A session on the local model. Holds its own message history."""

    name = "ollama"

    def __init__(self, system: str, history: list[dict] | None = None):
        self.system = system
        self.history: list[dict] = list(history or [])
        self._lock = asyncio.Lock()

    async def ask(self, text: str) -> TutorReply:
        async with self._lock:
            messages = [{"role": "system", "content": self.system},
                        *self.history[-MAX_HISTORY_MESSAGES:],
                        {"role": "user", "content": text}]
            try:
                r = await _ollama().post("/api/chat", json={
                    "model": config.OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "format": "json",           # forces a parseable reply
                    "keep_alive": config.OLLAMA_KEEP_ALIVE,
                    "options": {"temperature": 0.6, "num_predict": 250},
                })
            except httpx.TimeoutException as e:
                raise BrainError(f"The local model took longer than {OLLAMA_TIMEOUT}s.") from e
            except httpx.HTTPError as e:
                raise BrainError(f"Ollama is not reachable: {e}") from e
            if r.status_code != 200:
                raise BrainError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
            raw = ((r.json().get("message") or {}).get("content") or "").strip()
            reply = parse(raw)
            self.history += [{"role": "user", "content": text},
                             {"role": "assistant", "content": raw}]
            return reply

    async def close(self):
        pass  # nothing to tear down; Ollama unloads the model after keep_alive


# --- Choosing ------------------------------------------------------------------

class AutoBrain:
    """
    Claude while it works; the local model from the first failure on.

    The switch is one-way for the session, so a flaky connection does not make
    the tutor's voice and style flip back and forth mid-lesson. The local model
    is handed the conversation so far, so the learner does not start over.
    """

    def __init__(self, system: str):
        self.system = system
        self._claude = ClaudeBrain(system) if available() else None
        self._local: OllamaBrain | None = None
        self.history: list[dict] = []

    @property
    def name(self) -> str:
        return "ollama" if self._local or not self._claude else "claude"

    async def ask(self, text: str) -> TutorReply:
        if self._claude and not self._local:
            try:
                reply = await self._claude.ask(text)
                self.history += [{"role": "user", "content": text},
                                 {"role": "assistant", "content": reply.reply}]
                return reply
            except BrainError as e:
                log.warning("claude failed, switching this session to the local model: %s", e)
                await self._claude.close()
        if self._local is None:
            self._local = OllamaBrain(self.system, self.history)
        return await self._local.ask(text)

    async def close(self):
        if self._claude:
            await self._claude.close()


def make(system: str):
    """The brain for a new session, per TUTOR_BRAIN."""
    if config.BRAIN == "ollama":
        return OllamaBrain(system)
    if config.BRAIN == "claude":
        return ClaudeBrain(system)
    return AutoBrain(system)


def status() -> dict:
    return {
        "mode": config.BRAIN,
        "claude": {"available": available(), "model": config.MODEL},
        "ollama": {"available": ollama_available(), "model": config.OLLAMA_MODEL},
    }


async def shutdown():
    global _ollama_http
    if _ollama_http is not None:
        await _ollama_http.aclose()
        _ollama_http = None
