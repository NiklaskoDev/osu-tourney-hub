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
