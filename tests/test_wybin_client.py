import asyncio

import httpx
import pytest

from app.clients.http import USER_AGENT, make_http_client
from app.clients.wybin import API_BASE, InvalidWybinLink, WybinClient, parse_slug
from app.clients.wybin_models import (
    WybinFormatError,
    WybinNotFound,
    WybinUnavailable,
)
from app.services.links import map_id, match_id
from tests.support import FakeWybin


class Clock:
    """A monotonic clock the fake sleep advances, so spacing can be checked without waiting."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake() -> FakeWybin:
    fake = FakeWybin()
    fake.load_defaults()
    return fake


def make_client(fake: FakeWybin, clock: Clock, interval: float = 2) -> WybinClient:
    return WybinClient(
        make_http_client(fake.transport), interval, sleep=clock.sleep, monotonic=clock.monotonic
    )


def run(coroutine):
    return asyncio.run(coroutine)


# --- slug parsing ---


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("synthetic-cup", "synthetic-cup"),
        ("  4cwc25  ", "4cwc25"),
        ("https://wybin.xyz/4cwc25/home", "4cwc25"),
        ("https://wybin.xyz/4cwc25", "4cwc25"),
        ("wybin.xyz/4cwc25/rules", "4cwc25"),
        ("https://www.wybin.xyz/tournaments/CST/participants", "CST"),
        ("https://wybin.xyz/tournament/some.cup_2/home?x=1#top", "some.cup_2"),
    ],
)
def test_slugs_are_read_from_addresses_and_bare_names(text, slug):
    assert parse_slug(text) == slug


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://evil.example/4cwc25/home",  # another host: never fetched
        "http://wybin.xyz/4cwc25/home",  # not https
        "https://wybin.xyz.evil.example/4cwc25",
        "https://wybin.xyz/",
        "https://wybin.xyz/tournaments",
        "../etc/passwd",
        "slug with space",
        "-leading-dash",
        "a" * 101,
        "https://user@evil.example/x",
    ],
)
def test_anything_else_is_refused(text):
    with pytest.raises(InvalidWybinLink):
        parse_slug(text)


# --- requests ---


def test_a_tournament_is_fetched_from_the_documented_path_and_parsed(fake):
    client = make_client(fake, Clock())

    tournament = run(client.get_tournament("synthetic-cup"))

    assert tournament.wybin_id == 9001
    assert fake.requests[0].url == httpx.URL(f"{API_BASE}/tournament/synthetic-cup")
    assert fake.requests[0].method == "GET"


def test_requests_are_anonymous_and_identify_the_app(fake):
    run(make_client(fake, Clock()).get_tournament("synthetic-cup"))

    headers = fake.requests[0].headers
    assert headers["user-agent"] == USER_AGENT
    assert "authorization" not in headers
    assert "cookie" not in headers


def test_a_slug_cannot_change_the_path(fake):
    with pytest.raises(WybinNotFound):
        run(make_client(fake, Clock()).get_tournament("a/../../users-detailed/1"))

    assert fake.requests[0].url.raw_path.startswith(b"/api/v1/tournament/a%2F")


def test_pools_and_user_details_use_their_own_endpoints(fake):
    client = make_client(fake, Clock())

    pools = run(client.get_mappools(9001))
    detail = run(client.get_user_detailed(100011))

    assert set(pools) == {701, 702}
    assert len(detail.tournaments) == 2
    assert fake.paths() == ["/api/v1/tournament-mappools/9001", "/api/v1/users-detailed/100011"]


def test_an_unknown_tournament_is_not_found_and_not_retried(fake):
    with pytest.raises(WybinNotFound):
        run(make_client(fake, Clock()).get_tournament("nope"))

    assert len(fake.requests) == 1


def test_temporary_failures_are_retried_with_backoff_then_succeed(fake):
    clock = Clock()
    fake.failures = [503, 429]

    tournament = run(make_client(fake, clock, interval=0).get_tournament("synthetic-cup"))

    assert tournament.wybin_id == 9001
    assert len(fake.requests) == 3
    assert clock.sleeps == [1, 2]


def test_wybin_staying_down_ends_in_unavailable_after_three_tries(fake):
    fake.down = True

    with pytest.raises(WybinUnavailable):
        run(make_client(fake, Clock(), interval=0).get_tournament("synthetic-cup"))

    assert len(fake.requests) == 3


def test_a_transport_error_counts_as_a_failed_try():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("boom")

    client = WybinClient(make_http_client(httpx.MockTransport(handler)), 0, sleep=Clock().sleep)

    with pytest.raises(WybinUnavailable):
        run(client.get_tournament("x"))

    assert len(calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 302])
def test_other_client_errors_are_not_retried(fake, status):
    fake.failures = [status]

    with pytest.raises(WybinUnavailable):
        run(make_client(fake, Clock()).get_tournament("synthetic-cup"))

    assert len(fake.requests) == 1


def test_an_answer_that_is_not_json_is_a_format_error(fake):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>hi</html>"))
    client = WybinClient(make_http_client(transport), 0, sleep=Clock().sleep)

    with pytest.raises(WybinFormatError):
        run(client.get_tournament("x"))


def test_an_answer_in_an_unknown_shape_is_a_format_error(fake):
    fake.tournaments["synthetic-cup"] = {"id": 1}

    with pytest.raises(WybinFormatError):
        run(make_client(fake, Clock()).get_tournament("synthetic-cup"))


def test_an_oversized_answer_is_refused():
    big = httpx.Response(200, content=b"[" + b"1," * (5 * 1024 * 1024) + b"1]")
    client = WybinClient(
        make_http_client(httpx.MockTransport(lambda request: big)), 0, sleep=Clock().sleep
    )

    with pytest.raises(WybinFormatError, match="too large"):
        run(client.get_tournament("x"))


def test_requests_are_spaced_by_the_minimum_interval(fake):
    clock = Clock()
    client = make_client(fake, clock, interval=2)

    async def three() -> None:
        await client.get_tournament("synthetic-cup")
        await client.get_mappools(9001)
        await client.get_user_detailed(100011)

    run(three())

    assert clock.sleeps == [2, 2]  # the first request goes out at once


def test_concurrent_callers_are_spaced_too(fake):
    clock = Clock()
    client = make_client(fake, clock, interval=2)

    async def together() -> None:
        await asyncio.gather(
            client.get_tournament("synthetic-cup"),
            client.get_tournament("synthetic-solo"),
            client.get_mappools(9001),
        )

    run(together())

    assert clock.sleeps == [2, 2]
    assert len(fake.requests) == 3


# --- links ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("129891", 129891),
        ("  129891 ", 129891),
        ("/b/129891", 129891),
        ("https://osu.ppy.sh/b/129891", 129891),
        ("https://osu.ppy.sh/beatmaps/129891", 129891),
        ("https://osu.ppy.sh/beatmapsets/55#osu/129891", 129891),
        ("https://osu.ppy.sh/beatmapsets/55#fruits/777", 777),
        ("not a link", None),
        ("", None),
        ("https://osu.ppy.sh/beatmapsets/55", None),
    ],
)
def test_beatmap_ids_come_out_of_links(text, expected):
    assert map_id(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://osu.ppy.sh/community/matches/99001122", 99001122),
        ("osu.ppy.sh/mp/99001122", None),
        ("99001122", 99001122),
        (" 5 ", 5),
        ("", None),
        ("matches/", None),
    ],
)
def test_match_ids_come_out_of_links(text, expected):
    assert match_id(text) == expected
