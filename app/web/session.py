"""The signed session cookie: who is logged in, the CSRF token, one-shot messages, `next` URLs."""

import secrets
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Request

from app.domain.models import User

OSU_ID_KEY = "osu_id"
VERSION_KEY = "session_version"
CSRF_KEY = "csrf"
AUTH_AT_KEY = "auth_at"
NEXT_KEY = "next"
FLASH_KEY = "flash"
MAX_AGE_SECONDS = 14 * 24 * 3600
MAX_FLASHES = 3
REAUTH_WINDOW_SECONDS = 5 * 60
FlashKind = Literal["success", "warning", "error", "info"]


def establish_session(request: Request, user: User, now: datetime) -> None:
    """Start a fresh session for a user who just logged in: new CSRF token, nothing carried over."""
    request.session.clear()
    request.session.update(
        {
            OSU_ID_KEY: user.osu_id,
            VERSION_KEY: user.session_version,
            CSRF_KEY: secrets.token_urlsafe(32),
            AUTH_AT_KEY: int(now.timestamp()),
        }
    )


def seconds_since_login(request: Request, now: datetime) -> float | None:
    """Age of the last completed osu! login in this session, or None when unknown."""
    auth_at = request.session.get(AUTH_AT_KEY)
    if not isinstance(auth_at, int):
        return None
    return now.timestamp() - auth_at


def has_recent_login(request: Request, now: datetime) -> bool:
    """Sensitive actions (deleting data or a tournament) need a login from the last 5 minutes."""
    age = seconds_since_login(request, now)
    return age is not None and 0 <= age <= REAUTH_WINDOW_SECONDS


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = token
    return token


def flash(request: Request, kind: FlashKind, text: str) -> None:
    """Queue a message for the next page that is rendered."""
    messages: list[Any] = list(request.session.get(FLASH_KEY, []))
    messages.append({"kind": kind, "text": text})
    request.session[FLASH_KEY] = messages[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    messages = request.session.pop(FLASH_KEY, [])
    return [
        {"kind": m["kind"], "text": m["text"]}
        for m in messages
        if isinstance(m, dict) and isinstance(m.get("kind"), str) and isinstance(m.get("text"), str)
    ]


def safe_next(raw: object) -> str:
    """Accept only a path on this site. Anything else (absolute URL, `//host`, control chars)
    becomes `/`, so the login cannot be used as an open redirect."""
    if not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if "\\" in raw or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        return "/"
    parts = urlsplit(raw)
    return raw if not parts.scheme and not parts.netloc else "/"
