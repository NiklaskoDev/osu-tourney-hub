import re
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Connection

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.http import make_http_client
from app.clients.osu import OsuClient
from app.clients.ratelimit import RateLimiter
from app.config import Settings
from app.db.connection import open_db
from app.main import create_app
from tests.support import (
    ADMIN_ID,
    FakeOsu,
    FakeSheets,
    FakeWybin,
    NoRealNetwork,
    add_test_login,
)

BASE_URL = "https://tourney.example.com"  # secure cookies are only sent over https
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')


@dataclass
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC))


@pytest.fixture
def valid_env(tmp_path) -> dict[str, str]:
    """A complete, valid environment with dummy values and a temporary data directory."""
    return {
        "OSU_CLIENT_ID": "12345",
        "OSU_CLIENT_SECRET": "dummy-client-secret",
        "PUBLIC_URL": BASE_URL,
        "SESSION_SECRET": "s" * 32,
        "ADMIN_OSU_IDS": f"{ADMIN_ID},1002",
        "DATA_DIR": str(tmp_path / "data"),
    }


@pytest.fixture
def settings(valid_env) -> Settings:
    return Settings.from_env(valid_env)


@pytest.fixture
def osu() -> FakeOsu:
    return FakeOsu()


@pytest.fixture
def wybin() -> FakeWybin:
    return FakeWybin()


@pytest.fixture
def sheets() -> FakeSheets:
    return FakeSheets()


@pytest.fixture
def scan_client(settings, clock, osu) -> OsuClient:
    """An osu! client on the fake osu! with no rate limit and no waiting, for service tests."""

    async def no_sleep(seconds: float) -> None:
        return None

    return OsuClient(
        settings,
        make_http_client(osu.transport),
        RateLimiter(rate_per_min=60_000, burst=1000),
        clock,
        transport=osu.transport,
        sleep=no_sleep,
        scan_limiter=RateLimiter(rate_per_min=60_000, burst=1000),
    )


@pytest.fixture
def osu_delays() -> list[float]:
    """Back-off delays the osu! client asked for. Tests never really sleep."""
    return []


@pytest.fixture
def app(settings, clock, osu, wybin, sheets, osu_delays) -> FastAPI:
    network = NoRealNetwork(osu, wybin, sheets)
    built = create_app(settings, clock, http_transport=network.transport, start_workers=False)
    add_test_login(built)

    async def record_delay(seconds: float) -> None:
        osu_delays.append(seconds)

    async def no_sleep(seconds: float) -> None:
        return None

    # The real limiter allows 60 calls a minute after a burst of 5, which would slow down tests
    # that log in several times; back-off sleeping is recorded instead of waited for.
    built.state.osu._limiter = RateLimiter(rate_per_min=60_000, burst=1000)
    built.state.osu._scan_limiter = RateLimiter(rate_per_min=60_000, burst=1000)
    built.state.osu._sleep = record_delay
    built.state.wybin._sleep = no_sleep
    return built


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture
def db(client, settings) -> Iterator[Connection]:
    """A second connection to the app's database, for arranging and checking data."""
    with closing(open_db(settings.db_path)) as connection:
        yield connection


@pytest.fixture
def login(client) -> Callable[..., None]:
    """Log the shared client in as the given user (replacing any earlier session)."""

    def _login(osu_id: int, username: str = "") -> None:
        client.cookies.clear()
        response = client.get(f"/__test__/login/{osu_id}", params={"username": username})
        assert response.status_code == 200

    return _login


@pytest.fixture
def csrf(client) -> Callable[[], dict[str, str]]:
    """Form data with the logged-in session's CSRF token, read from the rendered profile page."""

    def _csrf() -> dict[str, str]:
        match = CSRF_INPUT.search(client.get("/me").text)
        assert match, "no CSRF token on /me; is a user logged in?"
        return {"csrf_token": match.group(1)}

    return _csrf
