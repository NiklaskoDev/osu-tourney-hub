from pathlib import Path

import pytest

from app.config import REQUIRED_VARIABLES, ConfigError, Settings


def test_valid_environment_is_parsed(valid_env):
    settings = Settings.from_env(valid_env)

    assert settings.osu_client_id == "12345"
    assert settings.osu_client_secret == "dummy-client-secret"
    assert settings.public_url == "https://tourney.example.com"
    assert settings.admin_osu_ids == frozenset({1001, 1002})
    assert settings.data_dir == Path(valid_env["DATA_DIR"])
    assert settings.db_path == Path(valid_env["DATA_DIR"]) / "app.db"
    assert settings.uses_https is True


def test_defaults_apply_for_optional_variables(valid_env):
    del valid_env["DATA_DIR"]

    settings = Settings.from_env(valid_env)

    assert settings.data_dir == Path("/data")
    assert settings.log_level == "info"


def test_from_env_reads_the_process_environment_by_default(valid_env, monkeypatch):
    for name, value in valid_env.items():
        monkeypatch.setenv(name, value)

    assert Settings.from_env().osu_client_id == "12345"


def test_all_missing_variables_are_named_in_one_error():
    with pytest.raises(ConfigError) as caught:
        Settings.from_env({})

    assert caught.value.missing == list(REQUIRED_VARIABLES)
    for name in REQUIRED_VARIABLES:
        assert name in str(caught.value)


@pytest.mark.parametrize("name", REQUIRED_VARIABLES)
def test_each_required_variable_is_enforced(valid_env, name):
    del valid_env[name]

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert caught.value.missing == [name]


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_value_counts_as_missing(valid_env, blank):
    valid_env["OSU_CLIENT_ID"] = blank

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert caught.value.missing == ["OSU_CLIENT_ID"]


def test_session_secret_must_have_at_least_32_characters(valid_env):
    valid_env["SESSION_SECRET"] = "x" * 31

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["SESSION_SECRET"]


def test_session_secret_of_exactly_32_characters_is_accepted(valid_env):
    valid_env["SESSION_SECRET"] = "x" * 32

    assert Settings.from_env(valid_env).session_secret == "x" * 32


def test_error_message_never_contains_secret_values(valid_env):
    valid_env["SESSION_SECRET"] = "tooshort-UNIQUE-secret"
    valid_env["OSU_CLIENT_SECRET"] = "client-UNIQUE-secret"
    valid_env["ADMIN_OSU_IDS"] = "not-a-number-UNIQUE"

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    message = str(caught.value)
    assert "SESSION_SECRET" in message
    assert "ADMIN_OSU_IDS" in message
    assert "UNIQUE" not in message


def test_settings_repr_hides_secrets(settings):
    text = repr(settings)

    assert settings.session_secret not in text
    assert settings.osu_client_secret not in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1001", {1001}),
        ("1001,1002", {1001, 1002}),
        (" 1001 , 1002 ,1003 ", {1001, 1002, 1003}),
        ("1001,,1002,", {1001, 1002}),
        ("7,7", {7}),
    ],
)
def test_admin_ids_are_parsed(valid_env, raw, expected):
    valid_env["ADMIN_OSU_IDS"] = raw

    assert Settings.from_env(valid_env).admin_osu_ids == frozenset(expected)


@pytest.mark.parametrize(
    "raw", ["abc", "1001,abc", "0", "1001,0", "-5", "1.5", "10 20", ",,", "١٢٣"]
)
def test_invalid_admin_ids_are_rejected(valid_env, raw):
    valid_env["ADMIN_OSU_IDS"] = raw

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["ADMIN_OSU_IDS"]


@pytest.mark.parametrize(
    "url",
    [
        "tourney.example.com",
        "ftp://tourney.example.com",
        "https://",
        "https://tourney.example.com/app",
        "https://tourney.example.com?x=1",
        "https://tourney.example.com#top",
        "https://tourney.example.com:notaport",
    ],
)
def test_invalid_public_url_is_rejected(valid_env, url):
    valid_env["PUBLIC_URL"] = url

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["PUBLIC_URL"]


def test_public_url_trailing_slash_is_removed_and_http_is_allowed(valid_env):
    valid_env["PUBLIC_URL"] = "http://localhost:8080/"

    settings = Settings.from_env(valid_env)

    assert settings.public_url == "http://localhost:8080"
    assert settings.uses_https is False


def test_log_level_is_case_insensitive_and_validated(valid_env):
    valid_env["LOG_LEVEL"] = "DEBUG"
    assert Settings.from_env(valid_env).log_level == "debug"

    valid_env["LOG_LEVEL"] = "chatty"
    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)
    assert list(caught.value.invalid) == ["LOG_LEVEL"]


def test_osu_api_rate_defaults_to_the_maximum(valid_env):
    assert Settings.from_env(valid_env).osu_api_rate_per_min == 60


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("30", 30), (" 60 ", 60), ("", 60)])
def test_osu_api_rate_is_read_from_the_environment(valid_env, raw, expected):
    valid_env["OSU_API_RATE_PER_MIN"] = raw

    assert Settings.from_env(valid_env).osu_api_rate_per_min == expected


@pytest.mark.parametrize("raw", ["0", "61", "-1", "fast", "30.5", "١٢"])
def test_osu_api_rate_outside_1_to_60_is_rejected(valid_env, raw):
    valid_env["OSU_API_RATE_PER_MIN"] = raw

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["OSU_API_RATE_PER_MIN"]


# --- wybin ---


def test_wybin_settings_default_to_30_minutes_and_2_seconds(valid_env):
    settings = Settings.from_env(valid_env)

    assert settings.wybin_poll_minutes == 30
    assert settings.wybin_min_interval_seconds == 2


def test_wybin_settings_are_read_from_the_environment(valid_env):
    valid_env.update({"WYBIN_POLL_MINUTES": "10", "WYBIN_MIN_INTERVAL_SECONDS": "60"})

    settings = Settings.from_env(valid_env)

    assert (settings.wybin_poll_minutes, settings.wybin_min_interval_seconds) == (10, 60)


@pytest.mark.parametrize("raw", ["9", "0", "-5", "abc", "10.5"])
def test_wybin_poll_below_10_minutes_or_not_a_number_is_rejected(valid_env, raw):
    valid_env["WYBIN_POLL_MINUTES"] = raw

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["WYBIN_POLL_MINUTES"]


@pytest.mark.parametrize("raw", ["0", "61", "x", "-1"])
def test_wybin_interval_outside_1_to_60_seconds_is_rejected(valid_env, raw):
    valid_env["WYBIN_MIN_INTERVAL_SECONDS"] = raw

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["WYBIN_MIN_INTERVAL_SECONDS"]


def test_capture_refresh_hours_defaults_to_24_and_is_read_from_the_environment(valid_env):
    assert Settings.from_env(valid_env).capture_refresh_hours == 24

    valid_env["CAPTURE_REFRESH_HOURS"] = "6"

    assert Settings.from_env(valid_env).capture_refresh_hours == 6


@pytest.mark.parametrize("raw", ["0", "-1", "x", "1.5"])
def test_capture_refresh_hours_below_one_or_not_a_number_is_rejected(valid_env, raw):
    valid_env["CAPTURE_REFRESH_HOURS"] = raw

    with pytest.raises(ConfigError) as caught:
        Settings.from_env(valid_env)

    assert list(caught.value.invalid) == ["CAPTURE_REFRESH_HOURS"]
