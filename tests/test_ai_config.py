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
            {"AI_PROVIDER": "anthropic", "AI_MODEL": "m", "AI_BASE_URL": "http://h:1"},
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
