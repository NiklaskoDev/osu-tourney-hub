"""wybin.xyz client: anonymous, read-only, polite.

Requests are spaced at least `min_interval` seconds apart (one gate for every caller), 429 and
5xx answers are retried with backoff, and a missing tournament is `WybinNotFound`. wybin has no
public API terms, so the app never writes to it and never sends credentials.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from app.clients.wybin_models import (
    WybinBracket,
    WybinFormatError,
    WybinMatch,
    WybinNotFound,
    WybinTournament,
    WybinUnavailable,
    WybinUserDetail,
    parse_mappool_stages,
    parse_matches,
    parse_tournament,
    parse_user_detail,
)

logger = logging.getLogger(__name__)

SITE_HOSTS = frozenset({"wybin.xyz", "www.wybin.xyz"})
API_BASE = "https://wybin.xyz/api/v1"
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SLUG_LENGTH = 100
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# First path segments on wybin.xyz that are pages, not tournaments.
_PAGE_SEGMENTS = frozenset({"tournaments", "tournament", "user", "users", "login", "api"})

Sleep = Callable[[float], Awaitable[None]]


class InvalidWybinLink(ValueError):
    """The text is neither a wybin tournament address nor a tournament slug."""


def parse_slug(text: str) -> str:
    """Tournament slug from a wybin address ("https://wybin.xyz/4cwc25/home",
    "wybin.xyz/tournaments/4cwc25") or a bare slug. Any other host is refused, so the app only
    ever talks to wybin.xyz."""
    value = text.strip()
    if not value:
        raise InvalidWybinLink("Enter a wybin address or tournament slug.")
    candidate = value
    if "/" in value:
        parts = urlsplit(value if "://" in value else f"https://{value}")
        if parts.scheme != "https" or parts.hostname not in SITE_HOSTS:
            raise InvalidWybinLink("Only https://wybin.xyz addresses are accepted.")
        segments = [unquote(part) for part in parts.path.split("/") if part]
        if segments and segments[0] in _PAGE_SEGMENTS:
            segments = segments[1:]
        if not segments:
            raise InvalidWybinLink("This address does not point to a tournament.")
        candidate = segments[0]
    if len(candidate) > MAX_SLUG_LENGTH or not _SLUG.match(candidate):
        raise InvalidWybinLink("This is not a valid wybin tournament slug.")
    return candidate


class WybinClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        min_interval: float,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._min_interval = min_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._gate = asyncio.Lock()
        self._last_request: float | None = None

    async def get_tournament(self, slug: str) -> WybinTournament:
        """The tournament with its stages, teams and players."""
        data = await self._get(f"/tournament/{quote(slug, safe='')}", "tournament")
        return _parsed(parse_tournament, data, "tournament")

    async def get_mappools(self, wybin_id: int) -> dict[int, list[WybinBracket]]:
        """Pools by wybin stage id; stages wybin sent no pool for are left out."""
        data = await self._get(f"/tournament-mappools/{wybin_id}", "mappools")
        return _parsed(parse_mappool_stages, data, "mappools")

    async def get_matches(self, wybin_id: int) -> list[WybinMatch]:
        """Every match of the tournament, with the stage it belongs to."""
        data = await self._get(f"/tournament-matches/{wybin_id}", "matches")
        return _parsed(parse_matches, data, "matches")

    async def get_user_detailed(self, wybin_user_id: int) -> WybinUserDetail:
        """The tournaments a wybin user signed up for."""
        data = await self._get(f"/users-detailed/{wybin_user_id}", "user")
        return _parsed(parse_user_detail, data, "user")

    async def _get(self, path: str, endpoint: str) -> Any:
        for attempt in range(MAX_ATTEMPTS):
            await self._wait_turn()
            try:
                response = await self._http.get(API_BASE + path)
            except httpx.TransportError as error:
                logger.warning("wybin %s request failed: %s", endpoint, type(error).__name__)
            else:
                outcome = _classify(response, endpoint)
                if outcome is not _RETRY:
                    return outcome
                logger.warning("wybin %s answered %s", endpoint, response.status_code)
            if attempt < MAX_ATTEMPTS - 1:
                await self._sleep(2**attempt)
        raise WybinUnavailable(f"wybin did not answer the {endpoint} request")

    async def _wait_turn(self) -> None:
        """Hold every caller until `min_interval` has passed since the previous request."""
        async with self._gate:
            if self._last_request is not None:
                wait = self._last_request + self._min_interval - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            self._last_request = self._monotonic()


_RETRY = object()


def _classify(response: httpx.Response, endpoint: str) -> Any:
    """The decoded JSON on success, `_RETRY` for a temporary failure, else raise."""
    status = response.status_code
    if status == 404:
        raise WybinNotFound(endpoint)
    if status == 429 or status >= 500:
        return _RETRY
    if status != 200:
        raise WybinUnavailable(f"wybin answered {status} for {endpoint}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise WybinFormatError(f"wybin {endpoint} answer is too large")
    try:
        return response.json()
    except ValueError as error:
        raise WybinFormatError(f"wybin {endpoint} answer is not JSON") from error


def _parsed(parser: Callable[[object], Any], data: object, endpoint: str) -> Any:
    try:
        return parser(data)
    except WybinFormatError as error:
        # Only the endpoint name and the reason are logged, never the payload.
        logger.warning("wybin %s answer not understood: %s", endpoint, error)
        raise
