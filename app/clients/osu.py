"""osu! API v2 client: login (OAuth2 authorization code flow) and user lookups.

The retry rules follow the reference script: 403/404 mean "not found", 429 and 5xx back off
exponentially, and after five tries the caller gets `OsuUnavailable`. Every request waits for the
shared rate limiter first. User tokens are used once to read `/me` and are never stored.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
from app.clock import Clock, utc_iso_or_none
from app.config import Settings
from app.domain.models import ApiScore, BeatmapMeta, FetchedMatch, MatchSummary, OsuProfile
from app.domain.modes import API_MODE_NAMES, canonical_mods

logger = logging.getLogger(__name__)

SITE = "https://osu.ppy.sh"
API_BASE = f"{SITE}/api/v2"
AUTHORIZE_URL = f"{SITE}/oauth/authorize"
TOKEN_URL = f"{SITE}/oauth/token"
CALLBACK_PATH = "/auth/callback"
LOGIN_SCOPE = "identify"
MAX_ATTEMPTS = 5
# Without this header osu! answers in the pre-2022 format: no `total_score`, no `ended_at`.
SCORE_API_VERSION = "20220705"
TOKEN_EXPIRY_MARGIN_S = 60
MAX_USERNAME_LENGTH = 64
MAX_METADATA_LENGTH = 200
AVATAR_HOSTS = frozenset({"a.ppy.sh", "assets.ppy.sh"})
MAX_API_SCORE = 2_000_000_000  # what the scores table accepts
# osu!lazer's Classic mod only changes how the score is displayed; tournaments ignore it like NF.
IGNORED_LAZER_MODS = frozenset({"CL"})

MATCH_LIST_LIMIT = 50  # the most /matches returns per page
MATCH_EVENTS_LIMIT = 100  # events per page of /matches/{id} (the API allows 101)
MATCH_MAX_PAGES = 10

# Lets a caller count the scan requests made inside its own task (a backfill, say) apart from the
# rest of the scanner's.
_scan_tally: ContextVar[list[int] | None] = ContextVar("scan_tally", default=None)

Sleep = Callable[[float], Awaitable[None]]


@contextmanager
def tally_scan_requests() -> Iterator[list[int]]:
    """Count the scan-priority requests the current task sends inside the block (retries
    included): `tally[0]` holds the number."""
    tally = [0]
    token = _scan_tally.set(tally)
    try:
        yield tally
    finally:
        _scan_tally.reset(token)


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
        scan_limiter: RateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._limiter = limiter
        # The scanner's own budget: requests at SCAN priority wait for it first, then for the
        # shared limiter, so the scanner can never take more than its share.
        self._scan_limiter = scan_limiter
        self.scan_requests = 0  # every request sent at SCAN priority, retries included
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
        answers 404, which is an empty list here, not an error. The request names an API version
        because osu! otherwise answers in the legacy score format, which `_parse_api_score`
        rejects.
        """
        url = f"{API_BASE}/beatmaps/{beatmap_id}/scores/users/{osu_id}/all"
        try:
            data = await self._app_get(
                url,
                priority,
                {"mode": API_MODE_NAMES[gamemode]},
                headers={"x-api-version": SCORE_API_VERSION},
            )
        except OsuNotFound:
            return []
        return _parse_api_scores(data, beatmap_id, osu_id, gamemode)

    # --- multiplayer lobbies --------------------------------------------------------------------

    async def list_matches(
        self,
        cursor_string: str | None = None,
        active: bool | None = None,
        priority: Priority = Priority.SCAN,
    ) -> tuple[list[MatchSummary], str | None]:
        """One page (newest first) of `GET /matches`: id, name and start/end time of each lobby.
        `active=True` lists only open lobbies. The second value continues the listing, or is None
        on the last page."""
        params = {"limit": str(MATCH_LIST_LIMIT), "sort": "id_desc"}
        if active:
            params["active"] = "true"
        if cursor_string:
            params["cursor_string"] = cursor_string
        data = await self._app_get(f"{API_BASE}/matches", priority, params)
        raw = data.get("matches")
        if not isinstance(raw, list):
            raise OsuUnavailable("osu! sent a match list in an unexpected shape")
        matches = [_parse_match_summary(item) for item in raw]
        next_cursor = data.get("cursor_string")
        return matches, next_cursor if isinstance(next_cursor, str) and next_cursor else None

    async def fetch_match(
        self,
        match_id: int,
        max_pages: int = MATCH_MAX_PAGES,
        priority: Priority = Priority.SCAN,
    ) -> FetchedMatch:
        """A lobby with its events, read backwards page by page (`before`) until the first event
        is reached or `max_pages` pages were read (then `truncated` is set and the oldest events
        are missing). Raises `OsuNotFound` for a deleted or private lobby."""
        url = f"{API_BASE}/matches/{match_id}"
        events: dict[int, dict[str, Any]] = {}
        match: MatchSummary | None = None
        before: int | None = None
        for _ in range(max_pages):
            params = {"limit": str(MATCH_EVENTS_LIMIT)}
            if before is not None:
                params["before"] = str(before)
            data = await self._app_get(url, priority, params)
            match = _parse_match_summary(data.get("match"))
            new = _new_events(data, events)
            first = data.get("first_event_id")
            if not new or (isinstance(first, int) and first in events):
                return FetchedMatch(match, _sorted(events), False)
            before = min(event["id"] for event in new)
        if match is None:
            raise OsuUnavailable("osu! was not asked for any match page")
        return FetchedMatch(match, _sorted(events), True)

    async def fetch_match_after(
        self,
        match_id: int,
        after_event_id: int,
        max_pages: int = MATCH_MAX_PAGES,
        priority: Priority = Priority.SCAN,
    ) -> FetchedMatch:
        """The events of a lobby newer than `after_event_id`, read forwards (`after`) until the
        latest event is reached. `truncated` means more are waiting than `max_pages` pages hold."""
        url = f"{API_BASE}/matches/{match_id}"
        events: dict[int, dict[str, Any]] = {}
        match: MatchSummary | None = None
        after = after_event_id
        for _ in range(max_pages):
            data = await self._app_get(
                url, priority, {"limit": str(MATCH_EVENTS_LIMIT), "after": str(after)}
            )
            match = _parse_match_summary(data.get("match"))
            new = _new_events(data, events)
            latest = data.get("latest_event_id")
            after = max([after, *(event["id"] for event in new)])
            if not new or (isinstance(latest, int) and after >= latest):
                return FetchedMatch(match, _sorted(events), False)
        if match is None:
            raise OsuUnavailable("osu! was not asked for any match page")
        return FetchedMatch(match, _sorted(events), True)

    async def _app_get(
        self,
        url: str,
        priority: Priority,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET with the app's own token; a rejected token is replaced once and the call retried."""
        try:
            return await self._request(
                "GET",
                url,
                bearer=await self.client_token(),
                params=params,
                headers=headers,
                priority=priority,
            )
        except OsuAuthError:
            self._token = None  # revoked or expired early: fetch a new token once and retry
            return await self._request(
                "GET",
                url,
                bearer=await self.client_token(),
                params=params,
                headers=headers,
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

    def _count_scan_request(self) -> None:
        self.scan_requests += 1
        tally = _scan_tally.get()
        if tally is not None:
            tally[0] += 1

    async def _request(
        self,
        method: str,
        url: str,
        *,
        bearer: str | None = None,
        params: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        priority: Priority = Priority.INTERACTIVE,
    ) -> dict[str, Any]:
        headers = dict(headers or {})
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"  # always wins over a caller's header
        path = urlsplit(url).path
        for attempt in range(MAX_ATTEMPTS):
            if priority == Priority.SCAN and self._scan_limiter is not None:
                await self._scan_limiter.acquire(priority)
            await self._limiter.acquire(priority)
            if priority == Priority.SCAN:
                self._count_scan_request()
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


def _parse_match_summary(item: object) -> MatchSummary:
    if not isinstance(item, dict):
        raise OsuUnavailable("osu! sent a match in an unexpected shape")
    match_id = _int_or_none(item.get("id"))
    if match_id is None or match_id <= 0:
        raise OsuUnavailable("osu! sent a match in an unexpected shape")
    name = item.get("name")
    return MatchSummary(
        id=match_id,
        name=name[:MAX_METADATA_LENGTH] if isinstance(name, str) else "",
        start_time=utc_iso_or_none(item.get("start_time")),
        end_time=utc_iso_or_none(item.get("end_time")),
    )


def _new_events(data: dict[str, Any], seen: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the page's events that are not in `seen` yet and return them."""
    raw = data.get("events")
    if not isinstance(raw, list):
        raise OsuUnavailable("osu! sent match events in an unexpected shape")
    new = []
    for event in raw:
        event_id = event.get("id") if isinstance(event, dict) else None
        if not isinstance(event_id, int) or isinstance(event_id, bool):
            raise OsuUnavailable("osu! sent a match event in an unexpected shape")
        if event_id not in seen:
            seen[event_id] = event
            new.append(event)
    return new


def _sorted(events: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [events[event_id] for event_id in sorted(events)]


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
    mods = api_mods(item.get("mods"), gamemode)
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
        achieved_at=utc_iso_or_none(item.get("ended_at")),
    )


def api_mods(raw: object, gamemode: int) -> str | None:
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
