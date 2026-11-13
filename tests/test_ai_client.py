"""The AI adapters: the requests they build, strict parsing of the answer, retries, and that the key
never reaches a log. Everything runs on httpx.MockTransport."""

import json
import logging
from copy import deepcopy

import httpx
import pytest

from app.clients import ai as ai_client
from app.clients.ai import (
    AiError,
    AnthropicProvider,
    NullProvider,
    OpenAICompatibleProvider,
    build_provider,
    parse_prediction,
)
from app.clients.http import make_http_client
from app.config import Settings
from app.domain.ai import MAX_REASONING_CHARS, PredictionContext
from tests.support import FakeAi, anthropic_reply, openai_reply

pytestmark = pytest.mark.anyio  # the anyio pytest plugin ships with starlette

KEY = "sk-test-0123456789-secret"
MODEL = "claude-sonnet-5-5"
SLOTS = frozenset({"NM1", "NM2", "HD1", "TB"})
CONTEXT = PredictionContext(
    {"teams": {"A": "Team A", "B": "Team B"}, "maps": [{"slot": "NM1"}]}, SLOTS
)
GOOD = {
    "winner": "A",
    "win_percent": 62.5,
    "bans": {"A": ["NM2"], "B": ["HD1"]},
    "picks": [{"team": "A", "slot": "NM1"}, {"team": "B", "slot": "TB"}],
    "reasoning": "Team A is ahead on the nomod maps.",
}


@pytest.fixture
def fake() -> FakeAi:
    return FakeAi()


def anthropic(fake: FakeAi, base_url: str = "") -> AnthropicProvider:
    http = make_http_client(httpx.MockTransport(fake.handle))
    return AnthropicProvider(http, MODEL, KEY, base_url)


def openai(fake: FakeAi, key: str = KEY, base_url: str = "") -> OpenAICompatibleProvider:
    http = make_http_client(httpx.MockTransport(fake.handle))
    return OpenAICompatibleProvider(http, "some-model", key, base_url)


def broken(**changes) -> dict:
    answer = deepcopy(GOOD)
    answer.update(changes)
    return answer


# --- parse_prediction --------------------------------------------------------------------------


def test_a_matching_answer_is_accepted():
    answer = parse_prediction(deepcopy(GOOD), SLOTS)

    assert answer.winner == "A"
    assert answer.win_percent == 62.5
    assert answer.bans == {"A": ("NM2",), "B": ("HD1",)}
    assert answer.picks == (("A", "NM1"), ("B", "TB"))
    assert answer.reasoning == "Team A is ahead on the nomod maps."


def test_the_boundaries_of_the_answer_are_accepted():
    answer = parse_prediction(
        broken(
            win_percent=50, bans={"A": [], "B": []}, picks=[], reasoning="x" * MAX_REASONING_CHARS
        ),
        SLOTS,
    )

    assert answer.win_percent == 50.0
    assert answer.picks == ()
    assert len(answer.reasoning) == MAX_REASONING_CHARS
    assert parse_prediction(broken(win_percent=100), SLOTS).win_percent == 100.0


@pytest.mark.parametrize(
    "answer",
    [
        "not an object",
        None,
        ["winner"],
        {k: v for k, v in GOOD.items() if k != "reasoning"},  # a field is missing
        {**GOOD, "extra": 1},  # a field too many
        broken(winner="C"),
        broken(winner="a"),
        broken(win_percent="62"),
        broken(win_percent=True),
        broken(win_percent=49.9),
        broken(win_percent=100.1),
        broken(win_percent=float("nan")),
        broken(bans={"A": ["NM2"]}),  # one team missing
        broken(bans={"A": ["NM2"], "B": ["HD1"], "C": []}),
        broken(bans={"A": "NM2", "B": []}),
        broken(bans={"A": ["XX9"], "B": []}),  # not in the pool
        broken(bans={"A": [1], "B": []}),
        broken(bans={"A": ["NM1", "NM2", "HD1", "TB", "NM1"], "B": []}),  # longer than the pool
        broken(picks="NM1"),
        broken(picks=[{"team": "A"}]),
        broken(picks=[{"team": "A", "slot": "NM1", "why": "x"}]),
        broken(picks=[{"team": "C", "slot": "NM1"}]),
        broken(picks=[{"team": "A", "slot": "XX9"}]),
        broken(picks=["NM1"]),
        broken(picks=[{"team": "A", "slot": "NM2"}]),  # NM2 is already banned
        broken(picks=[{"team": "A", "slot": "NM1"}, {"team": "B", "slot": "NM1"}]),
        broken(reasoning=""),
        broken(reasoning="   "),
        broken(reasoning=7),
        broken(reasoning="x" * (MAX_REASONING_CHARS + 1)),
    ],
)
def test_an_answer_that_does_not_match_is_refused(answer):
    with pytest.raises(AiError):
        parse_prediction(answer, SLOTS)


# --- the requests ------------------------------------------------------------------------------


async def test_the_anthropic_request_forces_the_schema_tool(fake):
    fake.replies.append(anthropic_reply(GOOD))

    answer = await anthropic(fake).predict(CONTEXT)

    assert answer.winner == "A"
    (request,) = fake.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = fake.bodies()[0]
    assert body["model"] == MODEL
    assert body["tool_choice"] == {"type": "tool", "name": "submit_estimate"}
    assert body["tools"][0]["input_schema"] == ai_client.PREDICTION_SCHEMA
    assert body["max_tokens"] == ai_client.ANTHROPIC_MAX_TOKENS
    assert body["system"] == ai_client.SYSTEM_PROMPT
    (message,) = body["messages"]
    assert message["role"] == "user"
    assert json.loads(message["content"]) == CONTEXT.data
    assert KEY not in request.content.decode()


async def test_the_anthropic_base_url_can_be_replaced(fake):
    fake.replies.append(anthropic_reply(GOOD))

    await anthropic(fake, "http://localhost:8000/proxy").predict(CONTEXT)

    assert str(fake.requests[0].url) == "http://localhost:8000/proxy/v1/messages"


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"content": [{"type": "text", "text": "A wins"}]}),
        httpx.Response(200, json={"content": []}),
        httpx.Response(200, json={}),
        httpx.Response(200, json=["x"]),
        httpx.Response(200, text="<html>"),
        anthropic_reply("not an object"),
        httpx.Response(
            200,
            json={"content": [{"type": "tool_use", "name": "another_tool", "input": GOOD}]},
        ),
    ],
)
async def test_an_anthropic_reply_without_the_estimate_is_refused(fake, reply):
    fake.replies.extend([reply, reply])

    with pytest.raises(AiError):
        await anthropic(fake).predict(CONTEXT)


async def test_the_openai_request_asks_for_a_json_schema(fake):
    fake.replies.append(openai_reply(GOOD))

    answer = await openai(fake).predict(CONTEXT)

    assert answer.winner == "A"
    (request,) = fake.requests
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = fake.bodies()[0]
    assert body["model"] == "some-model"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == ai_client.PREDICTION_SCHEMA
    system, user = body["messages"]
    assert system == {"role": "system", "content": ai_client.SYSTEM_PROMPT}
    assert json.loads(user["content"]) == CONTEXT.data


async def test_a_local_endpoint_is_called_without_a_key(fake):
    fake.replies.append(openai_reply(GOOD))

    await openai(fake, key="", base_url="http://localhost:11434/v1/").predict(CONTEXT)

    (request,) = fake.requests
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in request.headers


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "A wins"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "[1]"}}]}),
        httpx.Response(200, text="oops"),
        openai_reply(broken(winner="Z")),
    ],
)
async def test_an_openai_reply_without_the_estimate_is_refused(fake, reply):
    fake.replies.extend([reply, reply])

    with pytest.raises(AiError):
        await openai(fake).predict(CONTEXT)


async def test_a_request_waits_at_most_sixty_seconds(fake):
    fake.replies.append(anthropic_reply(GOOD))

    await anthropic(fake).predict(CONTEXT)

    timeout = fake.requests[0].extensions["timeout"]
    assert timeout["read"] == 60.0
    assert timeout["connect"] == 10.0


# --- retries -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 503])
async def test_a_failing_provider_is_tried_once_more(fake, status):
    fake.replies.extend([httpx.Response(status, text="busy"), anthropic_reply(GOOD)])

    answer = await anthropic(fake).predict(CONTEXT)

    assert answer.winner == "A"
    assert len(fake.requests) == 2


async def test_a_timeout_is_tried_once_more(fake):
    def times_out(request):
        raise httpx.ReadTimeout("slow", request=request)

    fake.replies.extend([times_out, anthropic_reply(GOOD)])

    assert (await anthropic(fake).predict(CONTEXT)).winner == "A"
    assert len(fake.requests) == 2


async def test_a_malformed_answer_is_tried_once_more(fake):
    fake.replies.extend([anthropic_reply(broken(winner="C")), anthropic_reply(GOOD)])

    assert (await anthropic(fake).predict(CONTEXT)).winner == "A"
    assert len(fake.requests) == 2


async def test_there_is_never_more_than_one_retry(fake):
    fake.replies.extend([httpx.Response(500)] * 5)

    with pytest.raises(AiError):
        await anthropic(fake).predict(CONTEXT)

    assert len(fake.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_a_refusal_by_the_provider_is_not_retried(fake, status):
    fake.replies.extend([httpx.Response(status, text="no"), anthropic_reply(GOOD)])

    with pytest.raises(AiError):
        await anthropic(fake).predict(CONTEXT)

    assert len(fake.requests) == 1


# --- secrets -----------------------------------------------------------------------------------


@pytest.mark.parametrize("make", [anthropic, openai])
async def test_the_key_never_reaches_the_log_or_the_error(fake, caplog, make):
    caplog.set_level(logging.DEBUG)
    # A provider that echoes the key back must not get it into the log either.
    fake.replies.extend([httpx.Response(401, text=f"bad key {KEY}")] * 2)

    with pytest.raises(AiError) as failure:
        await make(fake).predict(CONTEXT)

    assert KEY not in caplog.text
    assert KEY not in str(failure.value)
    assert "HTTP 401" in caplog.text  # the reason is logged, nothing more


@pytest.mark.parametrize(
    "message",
    [
        "tool_choice: forced tool use is not supported with thinking enabled",
        "Thinking may not be enabled when tool_choice forces tool use.",
    ],
)
async def test_a_400_about_thinking_models_is_logged_with_its_reason_only(fake, caplog, message):
    caplog.set_level(logging.DEBUG)
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    fake.replies.append(httpx.Response(400, json=body))

    with pytest.raises(AiError) as failure:
        await anthropic(fake).predict(CONTEXT)

    assert "extended thinking" in caplog.text and "claude-sonnet-5-5" in caplog.text
    assert message not in caplog.text and KEY not in caplog.text
    assert len(fake.requests) == 1  # a refused setup is not retried
    assert failure.value.retryable is False


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(400, json={"error": {"message": "max_tokens is too large"}}),
        httpx.Response(400, text="not json at all"),
        httpx.Response(400, json=["unexpected"]),
        httpx.Response(401, json={"error": {"message": "tool_choice thinking"}}),
    ],
)
async def test_any_other_failure_keeps_the_plain_status_reason(fake, caplog, reply):
    caplog.set_level(logging.DEBUG)
    fake.replies.append(reply)

    with pytest.raises(AiError):
        await anthropic(fake).predict(CONTEXT)

    assert "extended thinking" not in caplog.text
    assert f"HTTP {reply.status_code}" in caplog.text


async def test_a_dropped_connection_logs_no_url_or_key(fake, caplog):
    caplog.set_level(logging.DEBUG)

    def refuses(request):
        raise httpx.ConnectError(f"cannot connect to {request.url} with {KEY}", request=request)

    fake.replies.extend([refuses, refuses])

    with pytest.raises(AiError):
        await anthropic(fake).predict(CONTEXT)

    assert KEY not in caplog.text
    assert "ConnectError" in caplog.text


# --- which provider -----------------------------------------------------------------------------


def settings_with(valid_env, **ai_env) -> Settings:
    return Settings.from_env({**valid_env, **ai_env})


def test_no_provider_means_the_null_provider(valid_env):
    http = make_http_client()
    provider = build_provider(settings_with(valid_env), http)

    assert isinstance(provider, NullProvider)
    assert provider.enabled is False


async def test_the_null_provider_answers_nothing():
    with pytest.raises(AiError):
        await NullProvider().predict(CONTEXT)


@pytest.mark.parametrize(
    ("provider", "adapter"),
    [("anthropic", AnthropicProvider), ("openai", OpenAICompatibleProvider)],
)
def test_a_configured_provider_gets_its_adapter(valid_env, provider, adapter):
    env = {"AI_PROVIDER": provider, "AI_API_KEY": KEY, "AI_MODEL": MODEL}

    built = build_provider(settings_with(valid_env, **env), make_http_client())

    assert isinstance(built, adapter)
    assert built.enabled is True


def test_a_provider_without_a_model_or_key_stays_off(valid_env):
    http = make_http_client()
    no_model = {"AI_PROVIDER": "anthropic", "AI_API_KEY": KEY}
    no_key = {"AI_PROVIDER": "anthropic", "AI_MODEL": MODEL}

    assert isinstance(build_provider(settings_with(valid_env, **no_model), http), NullProvider)
    assert isinstance(build_provider(settings_with(valid_env, **no_key), http), NullProvider)
