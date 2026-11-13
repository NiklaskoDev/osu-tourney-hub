from datetime import timedelta

import httpx
import pytest

from app.clients.http import USER_AGENT, make_http_client
from app.clients.osu import (
    MAX_ATTEMPTS,
    OsuAuthError,
    OsuClient,
    OsuNotFound,
    OsuUnavailable,
)
from app.clients.ratelimit import Priority
from tests.support import APP_TOKEN

pytestmark = pytest.mark.anyio  # the anyio pytest plugin ships with starlette; no extra package


class RecordingLimiter:
    """Takes the place of the rate limiter and remembers who asked with which priority."""

    def __init__(self) -> None:
        self.priorities: list[Priority] = []

    async def acquire(self, priority: Priority) -> None:
        self.priorities.append(priority)


@pytest.fixture
def delays() -> list[float]:
    return []


@pytest.fixture
def limiter() -> RecordingLimiter:
    return RecordingLimiter()


@pytest.fixture
def osu_client(settings, clock, osu, limiter, delays) -> OsuClient:
    async def record(seconds: float) -> None:
        delays.append(seconds)

    http = make_http_client(osu.transport)
    return OsuClient(settings, http, limiter, clock, transport=osu.transport, sleep=record)


@pytest.fixture
def known_user(osu) -> int:
    osu.add_user(2002, "SampleUser", country="FR")
    return 2002


async def test_get_user_returns_the_public_profile(osu_client, known_user):
    profile = await osu_client.get_user(known_user)

    assert (profile.osu_id, profile.username, profile.country) == (2002, "SampleUser", "FR")
    assert profile.avatar_url == "https://a.ppy.sh/2002?1"


async def test_get_user_looks_up_by_id_with_the_app_token(osu_client, osu, known_user):
    await osu_client.get_user(known_user)

    (lookup,) = osu.calls("GET", "/users/2002")
    assert lookup.url.params["key"] == "id"
    assert lookup.headers["Authorization"] == f"Bearer {APP_TOKEN}"
    assert lookup.headers["User-Agent"] == USER_AGENT


async def test_client_token_request_sends_credentials_and_public_scope(osu_client, osu, known_user):
    await osu_client.get_user(known_user)

    (token_request,) = osu.calls("POST", "/oauth/token")
    body = token_request.content.decode()
    for expected in ("grant_type=client_credentials", "scope=public", "client_id=12345"):
        assert expected in body


@pytest.mark.parametrize("status", [403, 404])
async def test_forbidden_or_missing_user_is_not_found_without_retrying(osu_client, osu, status):
    await osu_client.client_token()
    osu.failures.append(status)

    with pytest.raises(OsuNotFound):
        await osu_client.get_user(777)

    assert len(osu.calls("GET", "/users/777")) == 1


async def test_server_errors_are_retried_with_exponential_backoff(
    osu_client, osu, known_user, delays
):
    await osu_client.client_token()
    osu.failures.extend([503, 429, 500])

    profile = await osu_client.get_user(known_user)

    assert profile.username == "SampleUser"
    assert delays == [1, 2, 4]


async def test_gives_up_after_five_tries(osu_client, osu, known_user, delays):
    await osu_client.client_token()
    osu.failures.extend([503] * 10)

    with pytest.raises(OsuUnavailable):
        await osu_client.get_user(known_user)

    assert len(osu.calls("GET", f"/users/{known_user}")) == MAX_ATTEMPTS
    assert delays == [1, 2, 4, 8]


async def test_network_errors_are_retried_too(settings, clock, limiter, delays, known_user):
    attempts = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("simulated", request=request)
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    async def record(seconds: float) -> None:
        delays.append(seconds)

    http = make_http_client(httpx.MockTransport(flaky))
    client = OsuClient(settings, http, limiter, clock, sleep=record)

    assert await client.client_token() == "t"
    assert delays == [1]


async def test_the_client_token_is_cached_until_a_minute_before_expiry(
    osu_client, osu, clock, known_user
):
    osu.token_lifetime = 3600
    await osu_client.get_user(known_user)
    await osu_client.get_user(known_user)
    assert len(osu.calls("POST", "/oauth/token")) == 1

    clock.current += timedelta(seconds=3600 - 60 - 1)
    await osu_client.get_user(known_user)
    assert len(osu.calls("POST", "/oauth/token")) == 1

    clock.current += timedelta(seconds=1)
    await osu_client.get_user(known_user)
    assert len(osu.calls("POST", "/oauth/token")) == 2


async def test_a_rejected_token_is_replaced_once(osu_client, osu, known_user):
    await osu_client.client_token()
    osu.reject_lookups = 1

    profile = await osu_client.get_user(known_user)

    assert profile.osu_id == known_user
    assert len(osu.calls("POST", "/oauth/token")) == 2


async def test_a_second_rejection_is_an_auth_error(osu_client, osu, known_user):
    osu.reject_lookups = 2

    with pytest.raises(OsuAuthError):
        await osu_client.get_user(known_user)

    assert len(osu.calls("POST", "/oauth/token")) == 2


async def test_bad_client_credentials_are_an_auth_error(osu_client, osu):
    osu.failures.append(401)

    with pytest.raises(OsuAuthError):
        await osu_client.client_token()


async def test_token_response_without_expiry_is_unusable(settings, clock, limiter):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "x"}))
    client = OsuClient(settings, make_http_client(transport), limiter, clock)

    with pytest.raises(OsuUnavailable):
        await client.client_token()


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 5},
        {"username": "NoId"},
        {"id": "5", "username": "StringId"},
        {"id": 0, "username": "Zero"},
        {"id": 5, "username": ""},
        {"id": 5, "username": "x" * 65},
        {"id": True, "username": "Bool"},
    ],
)
async def test_profiles_in_an_unexpected_shape_are_refused(osu_client, osu, payload):
    osu.profiles[5] = payload

    with pytest.raises(OsuUnavailable):
        await osu_client.get_user(5)


async def test_a_profile_for_a_different_id_is_refused(osu_client, osu):
    osu.profiles[5] = {"id": 6, "username": "Other"}

    with pytest.raises(OsuUnavailable):
        await osu_client.get_user(5)


async def test_avatars_outside_osu_image_hosts_are_dropped(osu_client, osu):
    osu.add_user(5, "Sample")
    for bad in ("https://evil.example/a.png", "http://a.ppy.sh/5", "javascript:alert(1)", 42):
        osu.profiles[5]["avatar_url"] = bad
        assert (await osu_client.get_user(5)).avatar_url is None
    osu.profiles[5]["avatar_url"] = "https://assets.ppy.sh/user-avatar.png"
    assert (await osu_client.get_user(5)).avatar_url == "https://assets.ppy.sh/user-avatar.png"


async def test_country_must_be_a_two_letter_code(osu_client, osu):
    osu.add_user(5, "Sample")
    osu.profiles[5]["country_code"] = "Germany"

    assert (await osu_client.get_user(5)).country is None


async def test_get_me_reads_the_profile_with_the_users_own_token(osu_client, osu):
    osu.add_user(2002, "LoginUser")

    profile = await osu_client.get_me("user-token-2002")

    assert profile.username == "LoginUser"
    assert osu.calls("GET", "/me")[0].headers["Authorization"] == "Bearer user-token-2002"


async def test_rejected_user_token_is_an_auth_error(osu_client):
    with pytest.raises(OsuAuthError):
        await osu_client.get_me("not-a-token")


async def test_every_request_waits_for_the_rate_limiter_with_its_priority(
    osu_client, limiter, known_user
):
    await osu_client.get_user(known_user, priority=Priority.SCAN)

    # one slot for the token request, one for the lookup
    assert limiter.priorities == [Priority.INTERACTIVE, Priority.SCAN]


async def test_a_non_json_answer_is_unavailable(settings, clock, limiter):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    client = OsuClient(settings, make_http_client(transport), limiter, clock)

    with pytest.raises(OsuUnavailable):
        await client.get_me("token")


async def test_other_client_errors_are_not_retried(settings, clock, limiter, delays):
    calls = []

    def teapot(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(418)

    client = OsuClient(settings, make_http_client(httpx.MockTransport(teapot)), limiter, clock)

    with pytest.raises(OsuUnavailable):
        await client.get_me("token")

    assert len(calls) == 1


# --- beatmaps (manual pools) ---


async def test_get_beatmap_returns_the_public_metadata(osu_client, osu):
    osu.add_beatmap(129891, artist="Synth Artist", title="Song")

    beatmap = await osu_client.get_beatmap(129891)

    assert (beatmap.beatmap_id, beatmap.beatmapset_id) == (129891, 130891)
    assert (beatmap.artist, beatmap.title, beatmap.version, beatmap.mapper) == (
        "Synth Artist",
        "Song",
        "Insane",
        "MapperMia",
    )
    assert (beatmap.star_rating, beatmap.bpm, beatmap.length_s) == (6.25, 180.0, 200)
    assert (beatmap.cs, beatmap.ar, beatmap.od, beatmap.hp) == (4.0, 9.0, 8.5, 5.0)


async def test_get_beatmap_uses_the_app_token_and_the_given_priority(osu_client, osu, limiter):
    osu.add_beatmap(7)

    await osu_client.get_beatmap(7, Priority.CAPTURE)

    (lookup,) = osu.calls("GET", "/beatmaps/7")
    assert lookup.headers["Authorization"] == f"Bearer {APP_TOKEN}"
    assert Priority.CAPTURE in limiter.priorities


async def test_an_unknown_beatmap_is_not_found(osu_client):
    with pytest.raises(OsuNotFound):
        await osu_client.get_beatmap(404404)


async def test_a_rejected_token_is_replaced_once_for_beatmaps_too(osu_client, osu):
    osu.add_beatmap(7)
    osu.failures = [401]

    beatmap = await osu_client.get_beatmap(7)

    assert beatmap.beatmap_id == 7
    assert len(osu.calls("POST", "/oauth/token")) == 2


async def test_a_beatmap_for_a_different_id_is_refused(osu_client, osu):
    osu.add_beatmap(7)
    osu.beatmaps[8] = osu.beatmaps[7]

    with pytest.raises(OsuUnavailable):
        await osu_client.get_beatmap(8)


@pytest.mark.parametrize(
    "payload",
    [{}, {"id": 7}, {"id": 7, "version": "x"}, {"id": "7", "version": "x", "beatmapset": {}}],
)
async def test_beatmaps_in_an_unexpected_shape_are_refused(osu_client, osu, payload):
    osu.beatmaps[7] = payload

    with pytest.raises(OsuUnavailable):
        await osu_client.get_beatmap(7)


async def test_missing_optional_beatmap_fields_become_none(osu_client, osu):
    osu.beatmaps[7] = {"id": 7, "version": "Easy", "beatmapset": {}}

    beatmap = await osu_client.get_beatmap(7)

    assert (beatmap.artist, beatmap.title, beatmap.mapper) == (None, None, None)
    assert (beatmap.star_rating, beatmap.bpm, beatmap.length_s, beatmap.cs) == (None,) * 4


# --- scores of one player on one beatmap (score capture) ---

CATCH, MANIA = 2, 3


async def test_user_beatmap_scores_are_mapped_to_the_apps_units(osu_client, osu):
    osu.add_api_score(
        1001,
        2002,
        9001,
        total_score=912_345,
        accuracy=0.98765,
        max_combo=700,
        mods=[{"acronym": "HD"}, {"acronym": "DT", "settings": {"speed_change": 1.5}}],
        statistics={"miss": 2},
        ended_at="2026-10-01T14:30:00+02:00",
    )

    (score,) = await osu_client.get_user_beatmap_scores(1001, 2002, 0)

    assert score.score_id == 9001
    assert (score.beatmap_id, score.osu_id) == (1001, 2002)
    assert score.total_score == 912_345  # the lazer standardised score, not legacy_total_score
    assert score.accuracy == 98.765  # a 0 to 1 fraction becomes a percentage
    assert score.max_combo == 700 and score.misses == 2
    assert score.mods == "DTHD"  # canonical: sorted acronyms
    assert score.achieved_at == "2026-10-01T12:30:00Z"  # converted to UTC


async def test_mods_may_be_plain_acronyms_and_classic_and_nofail_are_ignored(osu_client, osu):
    osu.add_api_score(1001, 2002, 9001, mods=["HR", "NF", "CL"])
    osu.add_api_score(1001, 2002, 9002, mods=[{"acronym": "CL"}])

    scores = await osu_client.get_user_beatmap_scores(1001, 2002, 0)

    assert [s.mods for s in scores] == ["HR", ""]


@pytest.mark.parametrize(
    ("statistics", "misses"),
    [({"miss": 4}, 4), ({"count_miss": 5}, 5), ({"miss": 1, "count_miss": 9}, 1), ({}, None)],
)
async def test_misses_are_read_from_either_statistics_key(osu_client, osu, statistics, misses):
    osu.add_api_score(1001, 2002, 9001, statistics=statistics)

    (score,) = await osu_client.get_user_beatmap_scores(1001, 2002, 0)

    assert score.misses == misses


async def test_unusable_scores_are_left_out_and_good_ones_kept(osu_client, osu):
    osu.add_api_score(1001, 2002, 9001)  # kept
    osu.add_api_score(1001, 2002, 9002, passed=False)
    osu.add_api_score(1001, 2002, 9003, accuracy=1.5)
    osu.add_api_score(1001, 2002, 9004, total_score=None)
    osu.add_api_score(1001, 2002, 9005, mods=[{"acronym": "WU"}])  # a mod osu! does not have
    osu.add_api_score(1001, 2002, 9006, mods=["RX"] + [{"acronym": 5}])
    osu.add_api_score(1001, 2002, 0)  # not a real score id
    del osu.api_scores[(1001, 2002)][0]["passed"]  # "passed" missing: not proven passed

    assert await osu_client.get_user_beatmap_scores(1001, 2002, 0) == []


async def test_a_missing_or_garbled_date_leaves_the_score_undated(osu_client, osu):
    osu.add_api_score(1001, 2002, 9001, ended_at=None)
    osu.add_api_score(1001, 2002, 9002, ended_at="last tuesday")
    osu.add_api_score(1001, 2002, 9003, ended_at="2026-10-01T12:00:00")  # no zone: read as UTC

    scores = await osu_client.get_user_beatmap_scores(1001, 2002, 0)

    assert [s.achieved_at for s in scores] == [None, None, "2026-10-01T12:00:00Z"]


async def test_the_mode_is_sent_by_its_api_name_and_mods_follow_the_mode(osu_client, osu):
    osu.add_api_score(1001, 2002, 9001, mods=["HR"])  # Hard Rock exists in catch ...
    osu.add_api_score(1001, 2002, 9002, mods=["K4"])  # ... a key mod does not

    scores = await osu_client.get_user_beatmap_scores(1001, 2002, CATCH)

    assert [s.score_id for s in scores] == [9001]
    assert osu.requests[-1].url.params["mode"] == "fruits"
    mania = await osu_client.get_user_beatmap_scores(1001, 2002, MANIA)
    assert [s.score_id for s in mania] == [9002]
    assert osu.requests[-1].url.params["mode"] == "mania"


async def test_a_map_osu_has_no_scores_for_gives_an_empty_list(osu_client, osu):
    assert await osu_client.get_user_beatmap_scores(7777, 2002, 0) == []  # the API answers 404


async def test_score_requests_use_the_capture_priority_unless_told_otherwise(
    osu_client, osu, limiter
):
    osu.add_api_score(1001, 2002, 9001)

    await osu_client.get_user_beatmap_scores(1001, 2002, 0)
    await osu_client.get_user_beatmap_scores(1001, 2002, 0, Priority.INTERACTIVE)

    assert limiter.priorities[-2:] == [Priority.CAPTURE, Priority.INTERACTIVE]


async def test_score_lookups_that_keep_failing_raise_unavailable(osu_client, osu, delays):
    osu.add_api_score(1001, 2002, 9001)
    osu.unavailable_paths.add("/api/v2/beatmaps/1001/scores/users/2002/all")

    with pytest.raises(OsuUnavailable):
        await osu_client.get_user_beatmap_scores(1001, 2002, 0)

    assert len(delays) == MAX_ATTEMPTS - 1


async def test_a_scores_answer_of_the_wrong_shape_raises_unavailable(osu_client, osu):
    osu.api_scores[(1001, 2002)] = {"not": "a list"}  # served as {"scores": {...}}

    with pytest.raises(OsuUnavailable):
        await osu_client.get_user_beatmap_scores(1001, 2002, 0)
