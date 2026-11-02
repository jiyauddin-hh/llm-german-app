"""
Capacity and fairness limits, and the local record of XP earned.

- At most MAX_SESSIONS conversations at once: each one is a claude process and
  shares one GPU with everything else on this PC.
- At most DAILY_MINUTES of tutor time per learner per day (server local date).

Usage and XP are kept in small JSON files under data/. The backend has no
endpoint for tutor XP yet (POST /user/progress/lesson would mark lessons
complete), so XP is logged here until it does.
"""

import asyncio
import datetime as dt
import json
import threading

from . import config

_active = 0
_active_users: set[int] = set()
_lock = asyncio.Lock()
_file_lock = threading.Lock()

USAGE_FILE = config.DATA_DIR / "usage.json"
XP_LOG = config.DATA_DIR / "xp_log.jsonl"


class LimitReached(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _today() -> str:
    return dt.date.today().isoformat()


def _load_usage() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    # Keep only today: older days are never read again.
    return {_today(): data.get(_today(), {})}


def seconds_used_today(user_id: int) -> float:
    with _file_lock:
        return float(_load_usage().get(_today(), {}).get(str(user_id), 0))


def add_usage(user_id: int, seconds: float):
    with _file_lock:
        data = _load_usage()
        day = data.setdefault(_today(), {})
        day[str(user_id)] = round(float(day.get(str(user_id), 0)) + max(0.0, seconds), 1)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(USAGE_FILE)


def remaining_seconds(user_id: int) -> float:
    """Tutor time left today. DAILY_MINUTES=0 means no daily limit."""
    if config.DAILY_MINUTES <= 0:
        return float("inf")
    return config.DAILY_MINUTES * 60 - seconds_used_today(user_id)


async def acquire(user_id: int):
    """Reserve a session slot, or raise LimitReached."""
    global _active
    async with _lock:
        if user_id in _active_users:
            raise LimitReached("already_active", "You already have a tutor session open.")
        if remaining_seconds(user_id) <= 0:
            raise LimitReached(
                "daily_limit",
                f"You've used today's {config.DAILY_MINUTES} minutes with Mira. See you tomorrow!",
            )
        if _active >= config.MAX_SESSIONS:
            raise LimitReached("busy", "Mira is busy with other learners. Please try again in a few minutes.")
        _active += 1
        _active_users.add(user_id)


async def release(user_id: int):
    global _active
    async with _lock:
        if user_id in _active_users:
            _active_users.discard(user_id)
            _active = max(0, _active - 1)


def active_sessions() -> int:
    return _active


def log_xp(entry: dict):
    with _file_lock:
        with XP_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
