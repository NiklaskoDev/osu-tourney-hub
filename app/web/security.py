"""Security response headers, CSRF protection and the small per-IP rate limiter."""

import secrets
from collections import deque
from datetime import datetime, timedelta

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.clock import Clock
from app.web.errors import UserFacingError
from app.web.session import CSRF_KEY

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' https://a.ppy.sh https://assets.ppy.sh data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self' https://osu.ppy.sh"
)
HSTS_VALUE = "max-age=31536000"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.headers = {
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        }
        if hsts:
            self.headers["Strict-Transport-Security"] = HSTS_VALUE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_MESSAGE = "Your session expired or this page is out of date. Reload the page and try again."


async def require_csrf(request: Request) -> None:
    """App-wide dependency: every state-changing request must carry the session's CSRF token.

    The token comes from the `X-CSRF-Token` header (htmx) or the hidden `csrf_token` form field.
    Because it is registered on the app, a new POST route cannot forget it.
    """
    if request.method not in UNSAFE_METHODS:
        return
    expected = request.session.get(CSRF_KEY)
    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None:
        form = await request.form()
        field = form.get(CSRF_FIELD)
        supplied = field if isinstance(field, str) else None
    if not isinstance(expected, str) or not expected or not supplied:
        raise UserFacingError(403, CSRF_MESSAGE)
    if not secrets.compare_digest(expected.encode(), supplied.encode()):
        raise UserFacingError(403, CSRF_MESSAGE)


class SlidingWindowLimiter:
    """Allows `limit` hits per key inside a trailing window. In memory, so it resets on restart."""

    MAX_TRACKED_KEYS = 4096

    def __init__(self, limit: int, window: timedelta, clock: Clock) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[datetime]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock.now()
        recent = self._hits.setdefault(key, deque())
        self._drop_expired(recent, now)
        if len(recent) >= self._limit:
            return False
        recent.append(now)
        if len(self._hits) > self.MAX_TRACKED_KEYS:
            self._sweep(now)
        return True

    def retry_after_seconds(self, key: str) -> int:
        recent = self._hits.get(key)
        if not recent:
            return 0
        wait = recent[0] + self._window - self._clock.now()
        return max(1, int(wait.total_seconds()) + 1)

    def _drop_expired(self, hits: deque[datetime], now: datetime) -> None:
        while hits and now - hits[0] >= self._window:
            hits.popleft()

    def _sweep(self, now: datetime) -> None:
        for key in list(self._hits):
            self._drop_expired(self._hits[key], now)
            if not self._hits[key]:
                del self._hits[key]


AUTH_LIMIT_PER_MINUTE = 20


async def limit_auth_requests(request: Request) -> None:
    """Dependency for `/auth/*`: at most 20 requests per minute from one client address."""
    limiter: SlidingWindowLimiter = request.app.state.auth_limiter
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        raise UserFacingError(
            429,
            "Too many login attempts. Wait a minute, then try again.",
            headers={"Retry-After": str(limiter.retry_after_seconds(client))},
        )
