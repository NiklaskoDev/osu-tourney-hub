"""osu! API v2 client: login (OAuth2 authorization code flow) and user lookups.

The retry rules follow the reference script: 403/404 mean "not found", 429 and 5xx back off
exponentially, and after five tries the caller gets `OsuUnavailable`. Every request waits for the
shared rate limiter first. User tokens are used once to read `/me` and are never stored.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.clients.http import REQUEST_TIMEOUT
from app.clients.ratelimit import Priority, RateLimiter
from app.clock import Clock, to_utc_iso
from app.config import Settings
from app.domain.models import ApiScore, BeatmapMeta, OsuProfile
from app.domain.modes import API_MODE_NAMES, canonical_mods

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
MAX_METADATA_LENGTH = 200
AVATAR_HOSTS = frozenset({"a.ppy.sh", "assets.ppy.sh"})
MAX_API_SCORE = 2_000_000_000  # what the scores table accepts
# osu!lazer's Classic mod only changes how the score is displayed; tournaments ignore it like NF.
IGNORED_LAZER_MODS = frozenset({"CL"})

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
        data = await self._app_get(f"{API_BASE}/users/{osu_id}", priority, {"key": "id"})
        profile = _parse_profile(data)
        if profile.osu_id != osu_id:
            raise OsuUnavailable("osu! returned a different user")
        return profile

    async def get_beatmap(
        self, beatmap_id: int, priority: Priority = Priority.INTERACTIVE
    ) -> BeatmapMeta:
        """Metadata of one beatmap, for manual pools. Raises `OsuNotFound` for an unknown map."""
        data = await self._app_get(f"{API_BASE}/beatmaps/{beatmap_id}", priority)
        beatmap = _parse_beatmap(data)
        if beatmap.beatmap_id != beatmap_id:
            raise OsuUnavailable("osu! returned a different beatmap")
        return beatmap

    async def get_user_beatmap_scores(
        self,
        beatmap_id: int,
        osu_id: int,
        gamemode: int,
        priority: Priority = Priority.CAPTURE,
    ) -> list[ApiScore]:
        """Every passed score of one player on one beatmap, one per mod combination they played.

        osu! only keeps scores for ranked and loved maps; for any other map or a hidden user it
        answers 404, which is an empty list here, not an error.
        """
        url = f"{API_BASE}/beatmaps/{beatmap_id}/scores/users/{osu_id}/all"
        try:
            data = await self._app_get(url, priority, {"mode": API_MODE_NAMES[gamemode]})
        except OsuNotFound:
            return []
        return _parse_api_scores(data, beatmap_id, osu_id, gamemode)

    async def _app_get(
        self, url: str, priority: Priority, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """GET with the app's own token; a rejected token is replaced once and the call retried."""
        try:
            return await self._request(
                "GET", url, bearer=await self.client_token(), params=params, priority=priority
            )
        except OsuAuthError:
            self._token = None  # revoked or expired early: fetch a new token once and retry
            return await self._request(
                "GET", url, bearer=await self.client_token(), params=params, priority=priority
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
        avatar_url=safe_avatar(data.get("avatar_url")),
        country=country if isinstance(country, str) and len(country) == 2 else None,
    )


def safe_avatar(value: object) -> str | None:
    """Keep only https avatar URLs on osu! image hosts, the same hosts the CSP allows."""
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    return value if parts.scheme == "https" and parts.hostname in AVATAR_HOSTS else None


def _parse_beatmap(data: dict[str, Any]) -> BeatmapMeta:
    beatmap_id = data.get("id")
    version = data.get("version")
    beatmapset = data.get("beatmapset")
    if not (
        isinstance(beatmap_id, int)
        and not isinstance(beatmap_id, bool)
        and beatmap_id > 0
        and isinstance(version, str)
        and isinstance(beatmapset, dict)
    ):
        raise OsuUnavailable("osu! sent a beatmap in an unexpected shape")
    return BeatmapMeta(
        beatmap_id=beatmap_id,
        beatmapset_id=_int_or_none(data.get("beatmapset_id")),
        artist=_text_or_none(beatmapset.get("artist")),
        title=_text_or_none(beatmapset.get("title")),
        version=version[:MAX_METADATA_LENGTH],
        mapper=_text_or_none(beatmapset.get("creator")),
        star_rating=_number_or_none(data.get("difficulty_rating")),
        bpm=_number_or_none(data.get("bpm")),
        length_s=_int_or_none(data.get("total_length")),
        cs=_number_or_none(data.get("cs")),
        ar=_number_or_none(data.get("ar")),
        od=_number_or_none(data.get("accuracy")),
        hp=_number_or_none(data.get("drain")),
    )


def _text_or_none(value: object) -> str | None:
    return value[:MAX_METADATA_LENGTH] if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _parse_api_scores(
    data: dict[str, Any], beatmap_id: int, osu_id: int, gamemode: int
) -> list[ApiScore]:
    """Keep the passed scores that fit the scores table; leave out any that do not (a mod this
    mode does not have, a missing or impossible number)."""
    raw = data.get("scores")
    if not isinstance(raw, list):
        raise OsuUnavailable("osu! sent scores in an unexpected shape")
    found = (_parse_api_score(item, beatmap_id, osu_id, gamemode) for item in raw)
    return [score for score in found if score is not None]


def _parse_api_score(item: object, beatmap_id: int, osu_id: int, gamemode: int) -> ApiScore | None:
    if not isinstance(item, dict) or item.get("passed") is not True:
        return None
    score_id = _int_or_none(item.get("id"))
    total = _int_or_none(item.get("total_score"))
    accuracy = _number_or_none(item.get("accuracy"))
    mods = _api_mods(item.get("mods"), gamemode)
    if (
        score_id is None
        or score_id <= 0
        or total is None
        or not 0 <= total <= MAX_API_SCORE
        or accuracy is None
        or not 0 <= accuracy <= 1
        or mods is None
    ):
        logger.debug("Left out an unusable osu! score on beatmap %s", beatmap_id)
        return None
    statistics = item.get("statistics")
    return ApiScore(
        score_id=score_id,
        beatmap_id=beatmap_id,
        osu_id=osu_id,
        mods=mods,
        total_score=total,
        accuracy=round(accuracy * 100, 4),
        max_combo=_count_or_none(item.get("max_combo")),
        misses=_misses(statistics if isinstance(statistics, dict) else {}),
        achieved_at=_utc_iso_or_none(item.get("ended_at")),
    )


def _api_mods(raw: object, gamemode: int) -> str | None:
    """Canonical mods from a list of acronyms or `{"acronym": ...}` objects; None when the mode
    does not have one of them."""
    if not isinstance(raw, list):
        return ""
    acronyms = []
    for mod in raw:
        acronym = mod.get("acronym") if isinstance(mod, dict) else mod
        if not isinstance(acronym, str):
            return None
        if acronym.upper() not in IGNORED_LAZER_MODS:
            acronyms.append(acronym)
    try:
        return canonical_mods(acronyms, gamemode)
    except ValueError:
        return None


def _misses(statistics: dict[str, Any]) -> int | None:
    """`miss` in lazer statistics, `count_miss` in the older shape."""
    for key in ("miss", "count_miss"):
        value = _count_or_none(statistics.get(key))
        if value is not None:
            return value
    return None


def _count_or_none(value: object) -> int | None:
    number = _int_or_none(value)
    return number if number is not None and number >= 0 else None


def _utc_iso_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return to_utc_iso(moment if moment.tzinfo else moment.replace(tzinfo=UTC))
