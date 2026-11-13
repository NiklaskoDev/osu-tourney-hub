"""Test doubles and database helpers shared by the test modules. Everything here is synthetic."""

import copy
import itertools
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.clients.osu import SCORE_API_VERSION
from app.clock import Clock, to_utc_iso
from app.domain.models import OsuProfile
from app.repos import users as users_repo
from app.web.deps import get_clock, get_db
from app.web.session import establish_session

FIXTURES = Path(__file__).parent / "fixtures"
ADMIN_ID = 1001  # first id in ADMIN_OSU_IDS of the test settings
USER_TOKEN_PREFIX = "user-token-"
APP_TOKEN = "app-token"
CODE_PREFIX = "code-"
DOWNLOAD_HOST = "doc-0s-6g-sheets.googleusercontent.com"  # where Google redirects a CSV export


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
        self.beatmaps: dict[int, dict] = {}
        self.api_scores: dict[tuple[int, int], list[dict]] = {}  # (beatmap id, user id) -> scores
        self.matches: dict[int, dict] = {}  # lobby id -> {"match": {...}, "events": [...]}
        self.match_statuses: dict[int, int] = {}  # lobby id -> status always answered instead
        self._event_ids = itertools.count(5000)
        self._game_ids = itertools.count(9000)
        self.transport = httpx.MockTransport(self._handle)

    def add_user(self, osu_id: int, username: str, country: str = "DE") -> None:
        self.profiles[osu_id] = {
            "id": osu_id,
            "username": username,
            "avatar_url": f"https://a.ppy.sh/{osu_id}?1",
            "country_code": country,
        }

    def add_beatmap(
        self, beatmap_id: int, artist: str = "Synth Artist", title: str = "Song"
    ) -> None:
        self.beatmaps[beatmap_id] = {
            "id": beatmap_id,
            "beatmapset_id": beatmap_id + 1000,
            "version": "Insane",
            "difficulty_rating": 6.25,
            "bpm": 180.0,
            "total_length": 200,
            "cs": 4.0,
            "ar": 9.0,
            "accuracy": 8.5,
            "drain": 5.0,
            "mode_int": 2,
            "beatmapset": {"artist": artist, "title": title, "creator": "MapperMia"},
        }

    def add_api_score(self, beatmap_id: int, user_id: int, score_id: int, **fields) -> dict:
        """A passed lazer score as `/beatmaps/{id}/scores/users/{user}/all` lists it. Keyword
        arguments replace or add keys (`accuracy=0.97`, `mods=[{"acronym": "HD"}]`, ...)."""
        score = {
            "id": score_id,
            "beatmap_id": beatmap_id,
            "user_id": user_id,
            "passed": True,
            "total_score": 850_000,
            "legacy_total_score": 1_200_000,
            "classic_total_score": 1_100_000,
            "legacy_score_id": None,
            "accuracy": 0.9876,
            "max_combo": 512,
            "mods": [],
            "statistics": {"great": 400, "miss": 3},
            "ended_at": "2026-10-01T12:34:56Z",
            **fields,
        }
        self.api_scores.setdefault((beatmap_id, user_id), []).append(score)
        return score

    def add_match(
        self,
        match_id: int,
        name: str = "Practice room",
        start_time: str = "2026-11-12T10:00:00+00:00",
        end_time: str | None = "2026-11-12T11:00:00+00:00",
    ) -> dict:
        """A multiplayer lobby as `/matches/{id}` and `/matches` show it. `end_time=None` is a lobby
        that is still open."""
        lobby = {
            "match": {"id": match_id, "name": name, "start_time": start_time, "end_time": end_time},
            "events": [],
        }
        self.matches[match_id] = lobby
        return lobby

    def add_game(
        self,
        match_id: int,
        beatmap_id: int,
        scores: list[tuple[int, int]] | None = None,
        mods: list[str] | None = None,
        mode_int: int = 0,
        scoring_type: str = "scorev2",
        ended: bool = True,
        accuracy: float = 0.98,
        score_mods: list[str] | None = None,
    ) -> dict:
        """Append a played game (an event) to a lobby; `scores` are (user id, score) pairs. With
        `ended=False` the game is still being played: no end time and no scores yet."""
        game_id = next(self._game_ids)
        game = {
            "id": game_id,
            "beatmap_id": beatmap_id,
            "start_time": "2026-11-12T10:10:00+00:00",
            "end_time": "2026-11-12T10:14:00+00:00" if ended else None,
            "mode": ["osu", "taiko", "fruits", "mania"][mode_int],
            "mode_int": mode_int,
            "scoring_type": scoring_type,
            "team_type": "head-to-head",
            "mods": mods or [],
            "scores": [
                {
                    "user_id": user_id,
                    "score": score,
                    "accuracy": accuracy,
                    "max_combo": 400,
                    "mods": score_mods or [],
                    "statistics": {"count_miss": 2},
                    "passed": True,
                    "match": {"slot": 0, "team": "none", "pass": True},
                }
                for user_id, score in (scores or [])
            ]
            if ended
            else [],
        }
        event = {
            "id": next(self._event_ids),
            "detail": {"type": "other"},
            "timestamp": "2026-11-12T10:14:00+00:00",
            "game": game,
        }
        self.matches[match_id]["events"].append(event)
        return event

    def finish_game(self, match_id: int, event: dict, scores: list[tuple[int, int]]) -> None:
        """A game that was in progress ends: the same event now has an end time and scores."""
        game = event["game"]
        game["end_time"] = "2026-11-12T10:14:00+00:00"
        game["scores"] = [
            {
                "user_id": user_id,
                "score": score,
                "accuracy": 0.97,
                "max_combo": 300,
                "mods": [],
                "statistics": {"count_miss": 1},
                "passed": True,
                "match": {"slot": 0, "team": "none", "pass": True},
            }
            for user_id, score in scores
        ]

    def match_requests(self, match_id: int) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/v2/matches/{match_id}"]

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
        if request.method == "GET" and path == "/api/v2/matches":
            return self._match_list(request)
        if request.method == "GET" and path.startswith("/api/v2/matches/"):
            return self._match(request, path.rsplit("/", 1)[1])
        if request.method == "GET" and path.startswith("/api/v2/beatmaps/"):
            if "/scores/users/" in path:
                return self._user_scores(request, path)
            return self._beatmap(request, path.rsplit("/", 1)[1])
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

    def _beatmap(self, request: httpx.Request, raw_id: str) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
            return httpx.Response(401)
        beatmap = self.beatmaps.get(int(raw_id))
        return httpx.Response(200, json=beatmap) if beatmap is not None else httpx.Response(404)

    def _user_scores(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
            return httpx.Response(401)
        # /api/v2/beatmaps/{beatmap}/scores/users/{user}/all
        _, _, beatmap, _, _, user, _ = path.removeprefix("/api/v2").split("/")
        scores = self.api_scores.get((int(beatmap), int(user)))
        if not scores:
            return httpx.Response(404)
        if int(request.headers.get("x-api-version", "0")) < int(SCORE_API_VERSION):
            scores = [_legacy_shape(score) for score in scores]
        return httpx.Response(200, json={"scores": scores})

    def _match_list(self, request: httpx.Request) -> httpx.Response:
        """`GET /matches`: newest first, `limit` per page, `cursor_string` continues below an id."""
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
            return httpx.Response(401)
        params = request.url.params
        below = int(params["cursor_string"]) if "cursor_string" in params else None
        wanted = sorted(self.matches, reverse=True)
        if params.get("active") == "true":
            wanted = [i for i in wanted if self.matches[i]["match"]["end_time"] is None]
        if below is not None:
            wanted = [i for i in wanted if i < below]
        limit = int(params.get("limit", "50"))
        page, more = wanted[:limit], len(wanted) > limit
        return httpx.Response(
            200,
            json={
                "matches": [self.matches[i]["match"] for i in page],
                "params": {"limit": limit, "sort": params.get("sort", "id_desc")},
                "cursor_string": str(page[-1]) if more else None,
            },
        )

    def _match(self, request: httpx.Request, raw_id: str) -> httpx.Response:
        """`GET /matches/{id}`: the latest `limit` events, or those `before`/`after` an event id."""
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
            return httpx.Response(401)
        match_id = int(raw_id)
        if match_id in self.match_statuses:
            return httpx.Response(self.match_statuses[match_id])
        lobby = self.matches.get(match_id)
        if lobby is None:
            return httpx.Response(404)
        params = request.url.params
        limit = int(params.get("limit", "100"))
        events = sorted(lobby["events"], key=lambda event: event["id"])
        if "after" in params:
            page = [e for e in events if e["id"] > int(params["after"])][:limit]
        elif "before" in params:
            page = [e for e in events if e["id"] < int(params["before"])][-limit:]
        else:
            page = events[-limit:]
        return httpx.Response(
            200,
            json={
                "match": lobby["match"],
                "events": copy.deepcopy(page),
                "users": [],
                "first_event_id": events[0]["id"] if events else None,
                "latest_event_id": events[-1]["id"] if events else None,
                "current_game_id": None,
            },
        )

    def _user(self, request: httpx.Request, raw_id: str) -> httpx.Response:
        if self.reject_lookups > 0:
            self.reject_lookups -= 1
            return httpx.Response(401)
        if request.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
            return httpx.Response(401)
        profile = self.profiles.get(int(raw_id))
        return httpx.Response(200, json=profile) if profile else httpx.Response(404)


def _legacy_shape(score: dict) -> dict:
    """What osu-web sends when `x-api-version` is missing: V1 `score`, no lazer totals."""
    dropped = ("total_score", "legacy_total_score", "classic_total_score", "ended_at")
    legacy = {key: value for key, value in score.items() if key not in dropped}
    legacy["score"] = score.get("legacy_total_score", 0)
    legacy["created_at"] = score.get("ended_at")
    return legacy


def load_fixture(name: str) -> dict:
    """A synthetic wybin response from tests/fixtures/wybin (a fresh copy every call)."""
    return json.loads((FIXTURES / "wybin" / f"{name}.json").read_text("utf-8"))


class FakeWybin:
    """Stands in for wybin.xyz/api/v1. No real network."""

    def __init__(self) -> None:
        self.tournaments: dict[str, dict] = {}  # by slug
        self.mappools: dict[int, dict] = {}  # by wybin tournament id
        self.matches: dict[int, dict] = {}  # by wybin tournament id
        self.missing_kinds: set[str] = (
            set()
        )  # endpoints (e.g. "tournament-matches") that answer 404
        self.users: dict[int, dict] = {}  # by wybin user id
        self.requests: list[httpx.Request] = []
        self.failures: list[int] = []  # statuses returned (once each) before normal service
        self.down = False  # every request answers 503
        self.transport = httpx.MockTransport(self.handle)

    def load_defaults(self) -> None:
        """The team tournament, the solo tournament, the pool, matches and one user's sign-ups.

        The team match fixture assumes `opponentOne`/`opponentTwo` are team objects: wybin's shape
        for team matches is not confirmed (only 1v1 is), so that part is synthetic guesswork."""
        team = load_fixture("tournament_team")
        solo = load_fixture("tournament_solo")
        self.tournaments = {team["slug"]: team, solo["slug"]: solo}
        # Like the real matches endpoint, a tournament without pool data answers an empty wrapper.
        self.mappools = {
            team["id"]: load_fixture("mappools"),
            solo["id"]: {"id": solo["id"], "stages": []},
        }
        self.matches = {
            team["id"]: load_fixture("matches_team"),
            solo["id"]: load_fixture("matches_solo"),
        }
        detail = load_fixture("user_detailed")
        self.users = {detail["id"]: detail}

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(503, text="<html>down</html>")
        if self.failures:
            return httpx.Response(self.failures.pop(0))
        prefix = "/api/v1/"
        if request.method != "GET" or not request.url.path.startswith(prefix):
            return httpx.Response(404, json={"status": 404})
        kind, _, key = request.url.path.removeprefix(prefix).partition("/")
        found: dict | None = None
        if kind == "tournament":
            found = self.tournaments.get(key)
        elif key.isdecimal() and kind == "tournament-mappools":
            found = self.mappools.get(int(key))
        elif key.isdecimal() and kind == "tournament-matches" and kind not in self.missing_kinds:
            # A tournament without match data answers an empty wrapper, like the real endpoint.
            found = self.matches.get(int(key), {"id": int(key), "stages": []})
        elif key.isdecimal() and kind == "users-detailed":
            found = self.users.get(int(key))
        if found is None:
            return httpx.Response(404, json={"status": 404, "error": "Not Found"})
        return httpx.Response(200, json=copy.deepcopy(found))


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


class FakeSheets:
    """Stands in for docs.google.com: the CSV export of public sheets. No real network."""

    def __init__(self) -> None:
        self.sheets: dict[str, str] = {}  # sheet id -> CSV text
        self.requests: list[httpx.Request] = []
        self.status = 200  # answered instead of the sheet when not 200
        self.content_type = "text/csv; charset=utf-8"
        self.redirect_to: str | None = None  # every request is redirected there
        self.redirect_once_to: str | None = None  # only the next request is
        self.download: str | None = None  # what Google's signed download host answers
        self.transport = httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [f"{request.url.path}?{request.url.query.decode()}" for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host.endswith("-sheets.googleusercontent.com") and self.download is not None:
            return httpx.Response(
                200, text=self.download, headers={"Content-Type": self.content_type}
            )
        if self.redirect_once_to is not None:
            target, self.redirect_once_to = self.redirect_once_to, None
            return httpx.Response(307, headers={"Location": target})
        if self.redirect_to is not None:
            return httpx.Response(307, headers={"Location": self.redirect_to})
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        parts = request.url.path.split("/")  # "", spreadsheets, d, <id>, export
        sheet = self.sheets.get(parts[3]) if len(parts) == 5 and parts[4] == "export" else None
        if sheet is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=sheet, headers={"Content-Type": self.content_type})


AI_HOSTS = ("api.anthropic.com", "api.openai.com", "localhost")


def anthropic_reply(answer: object) -> httpx.Response:
    """What the Messages API answers when the forced tool was called with `answer`."""
    block = {"type": "tool_use", "id": "toolu_1", "name": "submit_estimate", "input": answer}
    return httpx.Response(200, json={"type": "message", "content": [block]})


def openai_reply(answer: object) -> httpx.Response:
    """What a chat completions endpoint answers when the model replied with `answer` as JSON."""
    message = {"role": "assistant", "content": json.dumps(answer)}
    return httpx.Response(200, json={"choices": [{"index": 0, "message": message}]})


class FakeAi:
    """Stands in for the AI providers. Queue replies with `replies` (an `httpx.Response`, or a
    callable taking the request); with none left it answers 500. No real network."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: list = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            return httpx.Response(500, text="no reply queued")
        reply = self.replies.pop(0)
        return reply(request) if callable(reply) else reply

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


class NoRealNetwork:
    """Routes each request to the fake of its host. Any other host fails the test: tests never
    reach the internet."""

    def __init__(
        self,
        osu: FakeOsu,
        wybin: FakeWybin,
        sheets: FakeSheets | None = None,
        ai: FakeAi | None = None,
    ) -> None:
        self._hosts = {"osu.ppy.sh": osu._handle, "wybin.xyz": wybin.handle}
        if ai is not None:
            self._hosts.update({host: ai.handle for host in AI_HOSTS})
        if sheets is not None:
            self._hosts["docs.google.com"] = sheets.handle
            self._hosts[DOWNLOAD_HOST] = sheets.handle
        self.transport = httpx.MockTransport(self._route)

    def _route(self, request: httpx.Request) -> httpx.Response:
        handler = self._hosts.get(request.url.host)
        if handler is None:
            raise AssertionError(f"a test tried to reach {request.url.host}")
        return handler(request)


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


def add_stage(
    db: sqlite3.Connection,
    tournament_id: int,
    name: str = "Qualifiers",
    sort_order: int = 1,
    released: bool = True,
    lineup_size: int | None = None,
) -> int:
    cursor = db.execute(
        "INSERT INTO stages (tournament_id, name, sort_order, is_qualifier, lineup_size, "
        "pool_released) VALUES (?, ?, ?, 0, ?, ?)",
        (tournament_id, name, sort_order, lineup_size, int(released)),
    )
    return int(cursor.lastrowid)


def add_pool_map(
    db: sqlite3.Connection,
    stage_id: int,
    slot: str,
    beatmap_id: int | None = None,
    required_mods: str = "",
    freemod: bool = False,
    tiebreaker: bool = False,
    bracket: str | None = None,
) -> int:
    """A pool map whose beatmap id defaults to 1000 + its sort position; title "Song <slot>"."""
    position = db.execute(
        "SELECT COUNT(*) FROM pool_maps WHERE stage_id = ?", (stage_id,)
    ).fetchone()[0]
    cursor = db.execute(
        "INSERT INTO pool_maps (stage_id, slot, bracket, required_mods, is_freemod, "
        "is_tiebreaker, sort_order, beatmap_id, artist, title, version, star_rating, bpm, "
        "length_s, cs, ar, od, hp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Synth Artist', ?, 'Insane', "
        "6.25, 180, 200, 4, 9, 8.5, 5)",
        (
            stage_id,
            slot,
            bracket or slot.rstrip("0123456789"),
            required_mods,
            int(freemod),
            int(tiebreaker),
            position + 1,
            beatmap_id if beatmap_id is not None else 1000 + position + 1,
            f"Song {slot}",
        ),
    )
    return int(cursor.lastrowid)


def add_score(
    db: sqlite3.Connection,
    beatmap_id: int,
    osu_id: int,
    score: int,
    accuracy: float = 97.0,
    mods: str = "",
    scoring: str = "v2",
    achieved_at: str | None = "2026-11-01T12:00:00Z",
    source: str = "manual",
    tournament_id: int | None = None,
    entered_by: int | None = None,
    gamemode: int = 0,
    passed: bool = True,
    source_ref: str | None = None,
) -> int:
    cursor = db.execute(
        "INSERT INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, passed, "
        "achieved_at, source, source_ref, entered_by, tournament_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-11-01T12:00:00Z')",
        (
            beatmap_id,
            osu_id,
            gamemode,
            mods,
            score,
            scoring,
            accuracy,
            int(passed),
            achieved_at,
            source,
            source_ref or f"ref-{beatmap_id}-{osu_id}-{score}-{mods}-{scoring}",
            entered_by,
            tournament_id,
        ),
    )
    return int(cursor.lastrowid)


def add_team(
    db: sqlite3.Connection,
    tournament_id: int,
    name: str,
    players: list[tuple] | None = None,
    ours: bool = False,
    wybin_team_id: int | None = None,
) -> int:
    """A team with `(osu_id, username[, timezone_iana[, offset_minutes]])` players. `ours` also
    makes it the tournament's own team."""
    cursor = db.execute(
        "INSERT INTO teams (tournament_id, wybin_team_id, name, is_ours) VALUES (?, ?, ?, ?)",
        (tournament_id, wybin_team_id, name, int(ours)),
    )
    team_id = int(cursor.lastrowid)
    for player in players or []:
        osu_id, username, *zone = player
        iana, offset = (zone + [None, None])[:2]
        db.execute(
            "INSERT INTO team_players (team_id, osu_id, username, timezone_iana, "
            "timezone_offset_min) VALUES (?, ?, ?, ?, ?)",
            (team_id, osu_id, username, iana, offset),
        )
    if ours:
        db.execute("UPDATE tournaments SET our_team_id = ? WHERE id = ?", (team_id, tournament_id))
    return team_id


def add_match(
    db: sqlite3.Connection,
    tournament_id: int,
    team1_id: int | None,
    team2_id: int | None,
    scheduled_at: str | None,
    stage_id: int | None = None,
    label: str | None = None,
    score1: int | None = None,
    score2: int | None = None,
    status: str = "scheduled",
) -> int:
    cursor = db.execute(
        "INSERT INTO matches (tournament_id, stage_id, label, team1_id, team2_id, scheduled_at, "
        "score1, score2, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'2026-11-01T00:00:00Z')",
        (tournament_id, stage_id, label, team1_id, team2_id, scheduled_at, score1, score2, status),
    )
    return int(cursor.lastrowid)


def add_availability(
    db: sqlite3.Connection,
    tournament_id: int,
    osu_id: int,
    slots: list[str],
    status: str = "yes",
) -> None:
    db.executemany(
        "INSERT OR REPLACE INTO availability (tournament_id, osu_id, slot_start, status) "
        "VALUES (?, ?, ?, ?)",
        [(tournament_id, osu_id, slot, status) for slot in slots],
    )
