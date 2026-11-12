"""Test doubles and database helpers shared by the test modules. Everything here is synthetic."""

import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.clock import Clock, to_utc_iso
from app.domain.models import OsuProfile
from app.repos import users as users_repo
from app.web.deps import get_clock, get_db
from app.web.session import establish_session

ADMIN_ID = 1001  # first id in ADMIN_OSU_IDS of the test settings
USER_TOKEN_PREFIX = "user-token-"
APP_TOKEN = "app-token"
CODE_PREFIX = "code-"


def code_for(osu_id: int) -> str:
    """The authorization code the fake osu! accepts for this user."""
    return f"{CODE_PREFIX}{osu_id}"


class FakeOsu:
    """Stands in for osu.ppy.sh: OAuth token exchange, /me and /users/{id}. No real network."""

    def __init__(self) -> None:
        self.profiles: dict[int, dict] = {}
        self.requests: list[httpx.Request] = []
        self.failures: list[int] = []  # statuses returned (once each) before normal service
        self.token_lifetime = 86400
        self.reject_lookups = 0  # user lookups answered with 401 before normal service
        self.unavailable_paths: set[str] = set()  # these URL paths always answer 503
        self.garbage_paths: set[str] = set()  # these URL paths answer 200 with a non-JSON body
        self.transport = httpx.MockTransport(self._handle)

    def add_user(self, osu_id: int, username: str, country: str = "DE") -> None:
        self.profiles[osu_id] = {
            "id": osu_id,
            "username": username,
            "avatar_url": f"https://a.ppy.sh/{osu_id}?1",
            "country_code": country,
        }

    def calls(self, method: str, path_suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path_suffix)]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            return httpx.Response(self.failures.pop(0))
        if request.url.path in self.garbage_paths:
            return httpx.Response(200, text="<html>not json</html>")
        if request.url.path in self.unavailable_paths:
            return httpx.Response(503, text="<html>Service Unavailable</html>")
        path = request.url.path
        if request.method == "POST" and path == "/oauth/token":
            return self._token(request)
        if request.method == "GET" and path == "/api/v2/me":
            return self._me(request)
        if request.method == "GET" and path.startswith("/api/v2/users/"):
            return self._user(request, path.rsplit("/", 1)[1])
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") == "client_credentials":
            body = {"access_token": APP_TOKEN, "token_type": "Bearer"}
            return httpx.Response(200, json={**body, "expires_in": self.token_lifetime})
        code = form.get("code", "")
        user_id = code.removeprefix(CODE_PREFIX)
        if code.startswith(CODE_PREFIX) and user_id.isdecimal() and int(user_id) in self.profiles:
            token = {"access_token": f"{USER_TOKEN_PREFIX}{user_id}", "token_type": "Bearer"}
            return httpx.Response(200, json={**token, "expires_in": 86400})
        return httpx.Response(400, json={"error": "invalid_grant"})

    def _me(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token.startswith(USER_TOKEN_PREFIX):
            return httpx.Response(401)
        return httpx.Response(200, json=self.profiles[int(token[len(USER_TOKEN_PREFIX) :])])

    def _user(self, request: httpx.Request, raw_id: str) -> httpx.Response:
        if self.reject_lookups > 0:
            self.reject_lookups -= 1
            return httpx.Response(401)
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
            return httpx.Response(401)
        profile = self.profiles.get(int(raw_id))
        return httpx.Response(200, json=profile) if profile else httpx.Response(404)


def osu_login(client, osu_id: int, next_url: str | None = None) -> httpx.Response:
    """Run the real login round trip against the fake osu!: start, then come back with a code."""
    client.cookies.clear()
    params = {"next": next_url} if next_url is not None else {}
    start = client.get("/auth/login", params=params, follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return client.get(
        "/auth/callback", params={"code": code_for(osu_id), "state": state}, follow_redirects=False
    )


def add_test_login(app: FastAPI) -> None:
    """Register `GET /__test__/login/{osu_id}`, which logs the caller in without osu!.

    Lives in the tests only: the app itself has no way to skip the osu! login.
    """

    @app.get("/__test__/login/{osu_id}")
    async def test_login(
        request: Request,
        osu_id: int,
        username: str = "",
        db: sqlite3.Connection = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> PlainTextResponse:
        profile = OsuProfile(osu_id, username or f"player{osu_id}", None, "DE")
        user = users_repo.record_login(db, profile, to_utc_iso(clock.now()))
        establish_session(request, user, clock.now())
        return PlainTextResponse("ok")


def add_user(db: sqlite3.Connection, osu_id: int, username: str | None = None) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (osu_id, username, created_at) VALUES (?, ?, ?)",
        (osu_id, username or f"player{osu_id}", "2026-01-01T00:00:00Z"),
    )


def add_tournament(
    db: sqlite3.Connection, name: str = "Spring Cup", status: str = "active", acronym: str = "SC"
) -> int:
    cursor = db.execute(
        "INSERT INTO tournaments (name, acronym, gamemode, is_solo, status, source, created_at) "
        "VALUES (?, ?, 0, 0, ?, 'manual', '2026-01-01T00:00:00Z')",
        (name, acronym, status),
    )
    return int(cursor.lastrowid)


def add_member(
    db: sqlite3.Connection,
    tournament_id: int,
    osu_id: int,
    role: str = "player",
    is_substitute: bool = False,
    temp_captain_until: str | None = None,
) -> None:
    add_user(db, osu_id)
    db.execute(
        "INSERT INTO tournament_members "
        "(tournament_id, osu_id, role, is_substitute, temp_captain_until, source) "
        "VALUES (?, ?, ?, ?, ?, 'manual')",
        (tournament_id, osu_id, role, int(is_substitute), temp_captain_until),
    )
