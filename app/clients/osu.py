"""osu! API v2 client: login (OAuth2 authorization code flow) and user lookups.

The retry rules follow the reference script: 403/404 mean "not found", 429 and 5xx back off
exponentially, and after five tries the caller gets `OsuUnavailable`. Every request waits for the
shared rate limiter first. User tokens are used once to read `/me` and are never stored.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.clients.http import REQUEST_TIMEOUT
from app.clients.ratelimit import Priority, RateLimiter
from app.clock import Clock
from app.config import Settings
from app.domain.models import OsuProfile

logger = logging.getLogger(__name__)

SITE = "https://osu.ppy.sh"
API_BASE = f"{SITE}/api/v2"
AUTHORIZE_URL = f"{SITE}/oauth/authorize"
TOKEN_URL = f"{SITE}/oauth/token"
CALLBACK_PATH = "/auth/callback"
LOGIN_SCOPE = "identify"
MAX_ATTEMPTS = 5
TOKEN_EXPIRY_MARGIN_S = 60
MAX_USERNAME_LENGTH = 64
AVATAR_HOSTS = frozenset({"a.ppy.sh", "assets.ppy.sh"})

Sleep = Callable[[float], Awaitable[None]]


class OsuError(Exception):
    """Base class for everything this client raises."""


class OsuNotFound(OsuError):
    """osu! answered 403 or 404: the user does not exist or is hidden."""


class OsuUnavailable(OsuError):
    """osu! did not give a usable answer, even after retries."""


class OsuAuthError(OsuError):
    """The login was refused or a token was rejected."""


class OsuClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        limiter: RateLimiter,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._http = http
        self._limiter = limiter
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._token_lock = asyncio.Lock()
        oauth = OAuth()
        oauth.register(
            "osu",
            client_id=settings.osu_client_id,
            client_secret=settings.osu_client_secret,
            authorize_url=AUTHORIZE_URL,
            access_token_url=TOKEN_URL,
            client_kwargs={
                "scope": LOGIN_SCOPE,
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": "client_secret_post",
                "timeout": REQUEST_TIMEOUT,
                "transport": transport,
            },
        )
        self._oauth = oauth.osu

    # --- login -------------------------------------------------------------------------------

    async def authorize_redirect(self, request: Request) -> RedirectResponse:
        """Start the login: store state and PKCE verifier in the session, send the user to osu!."""
        redirect_uri = self._settings.public_url + CALLBACK_PATH
        return await self._oauth.authorize_redirect(request, redirect_uri)

    async def complete_login(self, request: Request) -> OsuProfile:
        """Validate state, swap the code for a token and read the user's profile.

        Raises `OsuAuthError` for a refused or forged login and `OsuUnavailable` when osu! fails.
        """
        await self._limiter.acquire(Priority.INTERACTIVE)
        try:
            token = await self._oauth.authorize_access_token(request)
        except OAuthError as error:
            # The error code names what went wrong (mismatching_state, access_denied, ...).
            logger.warning("osu! login refused: %s", error.error)
            raise OsuAuthError from error
        except (httpx.HTTPError, ValueError) as error:  # ValueError: answer was not JSON
            logger.warning("osu! token exchange failed: %s", type(error).__name__)
            raise OsuUnavailable from error
        return await self.get_me(str(token["access_token"]))

    async def get_me(self, user_token: str) -> OsuProfile:
        data = await self._request("GET", f"{API_BASE}/me", bearer=user_token)
        return _parse_profile(data)

    # --- lookups with the app's own credentials -----------------------------------------------

    async def get_user(self, osu_id: int, priority: Priority = Priority.INTERACTIVE) -> OsuProfile:
        """Public profile by id. Raises `OsuNotFound` for an unknown or hidden user."""
        try:
            data = await self._fetch_user(osu_id, priority)
        except OsuAuthError:
            self._token = None  # revoked or expired early: fetch a new token once and retry
            data = await self._fetch_user(osu_id, priority)
        profile = _parse_profile(data)
        if profile.osu_id != osu_id:
            raise OsuUnavailable("osu! returned a different user")
        return profile

    async def _fetch_user(self, osu_id: int, priority: Priority) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{API_BASE}/users/{osu_id}",
            bearer=await self.client_token(),
            params={"key": "id"},
            priority=priority,
        )

    async def client_token(self) -> str:
        """Client-credentials token (scope public), cached until shortly before it expires."""
        async with self._token_lock:
            now = self._clock.now()
            if self._token and self._token_expires and now < self._token_expires:
                return self._token
            data = await self._request(
                "POST",
                TOKEN_URL,
                form={
                    "client_id": self._settings.osu_client_id,
                    "client_secret": self._settings.osu_client_secret,
                    "grant_type": "client_credentials",
                    "scope": "public",
                },
            )
            try:
                token = str(data["access_token"])
                lifetime = int(data["expires_in"])
            except (KeyError, TypeError, ValueError) as error:
                raise OsuUnavailable("osu! returned an unexpected token response") from error
            self._token = token
            self._token_expires = now + timedelta(seconds=lifetime - TOKEN_EXPIRY_MARGIN_S)
            return token

    # --- transport -----------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        bearer: str | None = None,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        priority: Priority = Priority.INTERACTIVE,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        path = urlsplit(url).path
        for attempt in range(MAX_ATTEMPTS):
            await self._limiter.acquire(priority)
            try:
                response = await self._http.request(
                    method, url, headers=headers, params=params, data=form
                )
            except httpx.TransportError as error:
                logger.warning("osu! %s %s failed: %s", method, path, type(error).__name__)
            else:
                outcome = _classify(response, path)
                if outcome is not None:
                    return outcome
                logger.warning("osu! %s %s answered %s", method, path, response.status_code)
            if attempt < MAX_ATTEMPTS - 1:
                await self._sleep(2**attempt)
        raise OsuUnavailable(f"osu! did not answer {path} after {MAX_ATTEMPTS} tries")


def _classify(response: httpx.Response, path: str) -> dict[str, Any] | None:
    """Return the JSON body on success, None when the call should be retried, else raise."""
    status = response.status_code
    if status in (403, 404):
        raise OsuNotFound(path)
    if status == 401:
        raise OsuAuthError(path)
    if status == 429 or status >= 500:
        return None
    if status != 200:
        raise OsuUnavailable(f"osu! answered {status} for {path}")
    try:
        data = response.json()
    except ValueError as error:
        raise OsuUnavailable(f"osu! sent invalid JSON for {path}") from error
    if not isinstance(data, dict):
        raise OsuUnavailable(f"osu! sent an unexpected response for {path}")
    return data


def _parse_profile(data: dict[str, Any]) -> OsuProfile:
    osu_id = data.get("id")
    username = data.get("username")
    valid = (
        isinstance(osu_id, int)
        and not isinstance(osu_id, bool)
        and osu_id > 0
        and isinstance(username, str)
        and 0 < len(username) <= MAX_USERNAME_LENGTH
    )
    if not valid:
        raise OsuUnavailable("osu! sent a profile in an unexpected shape")
    country = data.get("country_code")
    return OsuProfile(
        osu_id=osu_id,
        username=username,
        avatar_url=_safe_avatar(data.get("avatar_url")),
        country=country if isinstance(country, str) and len(country) == 2 else None,
    )


def _safe_avatar(value: object) -> str | None:
    """Keep only https avatar URLs on osu! image hosts, the same hosts the CSP allows."""
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    return value if parts.scheme == "https" and parts.hostname in AVATAR_HOSTS else None
