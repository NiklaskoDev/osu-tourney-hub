"""The AI_* variables: off unless fully set, strict where a value is wrong, key never shown."""

import pytest

from app.config import ConfigError, Settings

KEY = "sk-test-0123456789-secret"


def load(valid_env, **ai_env) -> Settings:
    return Settings.from_env({**valid_env, **ai_env})


def test_ai_is_off_by_default(valid_env):
    settings = load(valid_env)

    assert settings.ai_provider == ""
    assert settings.ai_enabled is False
    assert settings.ai_missing == []


@pytest.mark.parametrize("provider", ["anthropic", "openai", " Anthropic "])
def test_a_provider_with_a_model_and_key_switches_ai_on(valid_env, provider):
    settings = load(valid_env, AI_PROVIDER=provider, AI_MODEL="claude-sonnet-5-5", AI_API_KEY=KEY)

    assert settings.ai_enabled is True
    assert settings.ai_provider == provider.strip().lower()
    assert settings.ai_model == "claude-sonnet-5-5"
    assert settings.ai_api_key == KEY


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"AI_PROVIDER": "anthropic"}, ["AI_MODEL", "AI_API_KEY"]),
        ({"AI_PROVIDER": "anthropic", "AI_API_KEY": KEY}, ["AI_MODEL"]),
        ({"AI_PROVIDER": "anthropic", "AI_MODEL": "m"}, ["AI_API_KEY"]),
        # Only an OpenAI-compatible endpoint with its own address may go without a key.
        (
            {"AI_PROVIDER": "anthropic", "AI_MODEL": "m", "AI_BASE_URL": "http://localhost:1"},
            ["AI_API_KEY"],
        ),
        ({"AI_PROVIDER": "openai", "AI_MODEL": "m"}, ["AI_API_KEY"]),
    ],
)
def test_an_unfinished_setup_stays_off_and_names_what_is_missing(valid_env, env, missing):
    settings = load(valid_env, **env)

    assert settings.ai_enabled is False
    assert settings.ai_missing == missing


def test_a_local_endpoint_needs_no_key(valid_env):
    settings = load(
        valid_env, AI_PROVIDER="openai", AI_MODEL="llama3", AI_BASE_URL="http://localhost:11434/v1/"
    )

    assert settings.ai_enabled is True
    assert settings.ai_base_url == "http://localhost:11434/v1"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1",
        "https://192.168.1.20:11434/v1",
        "HTTPS://Api.Example.com/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://127.5.5.5/v1",
        "http://[::1]:11434/v1",
        "http://ollama.local:11434/v1",
        "http://ollama:11434/v1",
        "http://notlocal/v1",
        "http://Ollama:11434/v1",
        "http://host.docker.internal:11434/v1",
        "http://10.0.0.5:11434/v1",
        "http://10.255.255.255/v1",
        "http://172.16.0.1/v1",
        "http://172.31.255.255/v1",
        "http://192.168.1.20:11434/v1",
    ],
)
def test_plain_http_is_accepted_only_for_local_and_private_hosts(valid_env, url):
    settings = load(valid_env, AI_PROVIDER="openai", AI_MODEL="llama3", AI_BASE_URL=url)

    assert settings.ai_base_url == url


def test_the_message_for_plain_http_to_a_public_host_names_the_variable_and_the_rule(valid_env):
    with pytest.raises(ConfigError) as failure:
        load(valid_env, AI_BASE_URL="http://api.example.com/v1", AI_API_KEY=KEY)

    assert "names without a dot" in failure.value.invalid["AI_BASE_URL"]
    assert "api.example.com" not in str(failure.value)


def test_a_model_or_key_without_a_provider_changes_nothing(valid_env):
    settings = load(valid_env, AI_MODEL="m", AI_API_KEY=KEY)

    assert settings.ai_enabled is False
    assert settings.ai_missing == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AI_PROVIDER", "gemini"),
        ("AI_MODEL", "two words"),
        ("AI_MODEL", "m" * 101),
        ("AI_BASE_URL", "ftp://host/v1"),
        ("AI_BASE_URL", "localhost:11434"),
        ("AI_BASE_URL", "https://user:pass@host/v1"),
        ("AI_BASE_URL", "https://host/v1?key=1"),
        ("AI_BASE_URL", "https://host:notaport/v1"),
        # Plain http to a public host would send the key and the match data unencrypted.
        ("AI_BASE_URL", "http://api.example.com/v1"),
        ("AI_BASE_URL", "http://8.8.8.8:11434/v1"),
        ("AI_BASE_URL", "http://172.32.0.1/v1"),
        ("AI_BASE_URL", "http://172.15.255.255/v1"),
        ("AI_BASE_URL", "http://11.0.0.1/v1"),
        ("AI_BASE_URL", "http://192.169.0.1/v1"),
        ("AI_BASE_URL", "http://localhost.example.com/v1"),
        ("AI_BASE_URL", "http://notlocal.example/v1"),
        ("AI_BASE_URL", "http://ollama./v1"),
        ("AI_BASE_URL", "http://com./v1"),
        ("AI_BASE_URL", "http://8/v1"),
        ("AI_BASE_URL", "http://2130706433/v1"),
        ("AI_BASE_URL", "http://0x08080808/v1"),
        ("AI_BASE_URL", "http://1e3/v1"),
        ("AI_BASE_URL", "http://host.docker.internal.example.com/v1"),
        ("AI_BASE_URL", "http://[2001:db8::1]/v1"),
    ],
)
def test_a_wrong_value_stops_the_start_and_names_only_the_variable(valid_env, name, value):
    with pytest.raises(ConfigError) as failure:
        load(valid_env, **{name: value}, AI_API_KEY=KEY)

    assert name in failure.value.invalid
    assert KEY not in str(failure.value)
    assert value not in str(failure.value)


def test_the_key_is_not_in_the_settings_repr(valid_env):
    settings = load(valid_env, AI_PROVIDER="anthropic", AI_MODEL="m", AI_API_KEY=KEY)

    assert KEY not in repr(settings)
