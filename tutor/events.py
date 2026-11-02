"""
The live event log behind the Log page (/logs.html): what the server and every
session are doing, in plain English, with timings.

Kept in memory only (the last MAX_EVENTS), so this never writes learners'
words to disk; a restart clears it. Every event is also written to the normal
server log (.run/tutor.log) as one line.
"""

import itertools
import logging
import threading
import time
from collections import deque

MAX_EVENTS = 2000

log = logging.getLogger("tutor")

_events: deque = deque(maxlen=MAX_EVENTS)
_ids = itertools.count(1)
_lock = threading.Lock()


def add(message: str, *, step: str = "server", level: str = "info",
        session: str | None = None, learner: str | None = None, **details):
    """
    Record one event. `step` groups events on the Log page: server, connect,
    login, limits, lesson, brain, hear, think, reply, voice, end. `level` is
    info, ok, warning or error.
    """
    event = {
        "id": next(_ids),
        "at": time.time(),
        "step": step,
        "level": level,
        "session": session,
        "learner": learner,
        "message": message,
        "details": {k: v for k, v in details.items() if v is not None},
    }
    with _lock:
        _events.append(event)
    line = f"[{session or 'server'}] {message}"
    if level == "error":
        log.error(line)
    elif level == "warning":
        log.warning(line)
    else:
        log.info(line)


def last_id() -> int:
    """Id of the newest event (0 if none). Lower than before = the server restarted."""
    with _lock:
        return _events[-1]["id"] if _events else 0


def after(last_id: int, limit: int = 500) -> list[dict]:
    """Events newer than last_id, oldest first."""
    with _lock:
        return [e for e in _events if e["id"] > last_id][-limit:]


class SessionLog:
    """Events for one session, tagged with its id (S1, S2, ...) and learner."""

    _numbers = itertools.count(1)

    def __init__(self):
        self.id = f"S{next(self._numbers)}"
        self.learner: str | None = None

    def __call__(self, step: str, message: str, level: str = "info", **details):
        add(message, step=step, level=level, session=self.id, learner=self.learner, **details)
