"""
Client for the German app's Laravel backend.

Every call is made *as the learner*, with the Bearer token the app sent us. So
the backend decides who they are and what they may read — this service holds
no user database of its own and cannot see anyone else's data.

Only read endpoints are used. POST /user/progress/lesson is deliberately not
called: it marks a lesson completed, which a conversation must not do.
"""

import hashlib
import time

import httpx

from . import config


class Unauthorized(Exception):
    """The backend rejected the learner's token (HTTP 401)."""


class BackendError(Exception):
    """The backend could not be reached or answered with an error."""


_client: httpx.AsyncClient | None = None
_profiles: dict[str, tuple[float, dict]] = {}


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=config.GERMAN_API_BASE, timeout=10)
    return _client


async def close():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _headers(token: str) -> dict:
    return {
        "Accept": "application/json",
        "x-api-key": config.GERMAN_API_KEY,
        "Authorization": f"Bearer {token}",
    }


async def _get(path: str, token: str) -> dict:
    try:
        r = await _http().get(path, headers=_headers(token))
    except httpx.HTTPError as e:
        raise BackendError(f"German backend unreachable: {e}") from e
    if r.status_code == 401:
        raise Unauthorized("Login expired or invalid.")
    if r.status_code == 403:
        raise BackendError("German backend rejected the API key (403).")
    if r.status_code == 404:
        return {}
    if r.status_code >= 400:
        raise BackendError(f"German backend returned HTTP {r.status_code} for {path}")
    try:
        return r.json()
    except ValueError as e:
        raise BackendError(f"German backend sent non-JSON for {path}") from e


def _cache_key(token: str) -> str:
    # Never keep raw tokens in memory longer than the request needs them.
    return hashlib.sha256(token.encode()).hexdigest()


async def profile(token: str) -> dict:
    """
    The learner's user record (with .profile), or raises Unauthorized.

    This is the login check: a token the backend accepts is a real learner.
    """
    key = _cache_key(token)
    hit = _profiles.get(key)
    if hit and time.monotonic() - hit[0] < config.PROFILE_CACHE_SECONDS:
        return hit[1]
    data = await _get("/user/profile", token)
    user = data.get("user") if isinstance(data, dict) else None
    if not user or "id" not in user:
        raise BackendError("Profile response had no user.")
    _profiles[key] = (time.monotonic(), user)
    return user


async def lesson(token: str, chapter: int, lesson_no: int) -> dict:
    """The lesson with its ordered questions, or {} if it does not exist."""
    return await _get(f"/questions/chapter/{int(chapter)}/lesson/{int(lesson_no)}", token)


async def dashboard(token: str) -> dict:
    """Completed lessons per chapter. Best effort: {} on any failure but 401."""
    try:
        data = await _get("/user/progress/dashboard", token)
    except BackendError:
        return {}
    return data.get("data") or {} if isinstance(data, dict) else {}
