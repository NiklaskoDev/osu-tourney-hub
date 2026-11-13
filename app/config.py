"""Settings read from environment variables.

Errors name the variables that are wrong and never echo their values.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REQUIRED_VARIABLES = (
    "OSU_CLIENT_ID",
    "OSU_CLIENT_SECRET",
    "PUBLIC_URL",
    "SESSION_SECRET",
    "ADMIN_OSU_IDS",
)
MIN_SESSION_SECRET_LENGTH = 32
DEFAULT_DATA_DIR = "/data"
DEFAULT_LOG_LEVEL = "info"
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
# osu! asks API users to stay under 60 requests per minute, so this is also the ceiling.
MAX_OSU_API_RATE_PER_MIN = 60
DEFAULT_WYBIN_POLL_MINUTES = 30
MIN_WYBIN_POLL_MINUTES = 10
DEFAULT_WYBIN_MIN_INTERVAL_SECONDS = 2
MAX_WYBIN_MIN_INTERVAL_SECONDS = 60
DEFAULT_CAPTURE_REFRESH_HOURS = 24
DEFAULT_LIVE_POLL_SECONDS = 10
MIN_LIVE_POLL_SECONDS = 5
DEFAULT_SCANNER_BUDGET_PER_MIN = 30
MIN_SCANNER_WORKERS = 1
MAX_SCANNER_WORKERS = 8
DEFAULT_SCANNER_WORKERS = 4
DEFAULT_SCANNER_MAX_BACKLOG = 5000
DEFAULT_SCANNER_OPEN_MAX_HOURS = 24
AI_PROVIDERS = ("anthropic", "openai")  # "openai" = any OpenAI-compatible chat completions endpoint
MAX_AI_MODEL_LENGTH = 100
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


class ConfigError(Exception):
    """The environment does not describe a usable configuration."""

    def __init__(self, missing: list[str], invalid: dict[str, str]) -> None:
        self.missing = missing
        self.invalid = invalid
        parts = []
        if missing:
            parts.append("missing required variables: " + ", ".join(missing))
        if invalid:
            parts.append(
                "invalid variables: " + ", ".join(f"{k} ({v})" for k, v in invalid.items())
            )
        super().__init__("Configuration error - " + "; ".join(parts))


@dataclass(frozen=True)
class Settings:
    osu_client_id: str
    osu_client_secret: str = field(repr=False)
    public_url: str
    session_secret: str = field(repr=False)
    admin_osu_ids: frozenset[int]
    data_dir: Path
    log_level: str
    osu_api_rate_per_min: int
    wybin_poll_minutes: int = DEFAULT_WYBIN_POLL_MINUTES
    wybin_min_interval_seconds: int = DEFAULT_WYBIN_MIN_INTERVAL_SECONDS
    capture_refresh_hours: int = DEFAULT_CAPTURE_REFRESH_HOURS
    live_poll_seconds: int = DEFAULT_LIVE_POLL_SECONDS
    scanner_enabled: bool = True
    scanner_budget_per_min: int = DEFAULT_SCANNER_BUDGET_PER_MIN
    scanner_workers: int = DEFAULT_SCANNER_WORKERS
    scanner_max_backlog: int = DEFAULT_SCANNER_MAX_BACKLOG
    scanner_open_max_hours: int = DEFAULT_SCANNER_OPEN_MAX_HOURS
    ai_provider: str = ""
    ai_api_key: str = field(default="", repr=False)
    ai_model: str = ""
    ai_base_url: str = ""

    @property
    def ai_missing(self) -> list[str]:
        """Names of the AI variables still needed to switch AI predictions on, once a provider is
        chosen. A local OpenAI-compatible endpoint (`AI_BASE_URL`) needs no key."""
        if not self.ai_provider:
            return []
        missing = []
        if not self.ai_model:
            missing.append("AI_MODEL")
        if not self.ai_api_key and not (self.ai_provider == "openai" and self.ai_base_url):
            missing.append("AI_API_KEY")
        return missing

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_provider) and not self.ai_missing

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def uses_https(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        environ = os.environ if environ is None else environ
        values = {name: environ.get(name, "").strip() for name in REQUIRED_VARIABLES}
        missing = [name for name in REQUIRED_VARIABLES if not values[name]]
        invalid: dict[str, str] = {}

        public_url = values["PUBLIC_URL"]
        if public_url and not _is_origin(public_url):
            invalid["PUBLIC_URL"] = "must look like https://host[:port] without a path"

        session_secret = values["SESSION_SECRET"]
        if session_secret and len(session_secret) < MIN_SESSION_SECRET_LENGTH:
            invalid["SESSION_SECRET"] = f"must be at least {MIN_SESSION_SECRET_LENGTH} characters"

        admin_ids: frozenset[int] = frozenset()
        if values["ADMIN_OSU_IDS"]:
            parsed = _parse_admin_ids(values["ADMIN_OSU_IDS"])
            if parsed is None:
                invalid["ADMIN_OSU_IDS"] = "must be comma-separated positive numbers"
            else:
                admin_ids = parsed

        log_level = environ.get("LOG_LEVEL", "").strip().lower() or DEFAULT_LOG_LEVEL
        if log_level not in LOG_LEVELS:
            invalid["LOG_LEVEL"] = "must be one of " + ", ".join(LOG_LEVELS)

        osu_api_rate = _parse_rate(environ.get("OSU_API_RATE_PER_MIN", ""))
        if osu_api_rate is None:
            invalid["OSU_API_RATE_PER_MIN"] = (
                f"must be a number from 1 to {MAX_OSU_API_RATE_PER_MIN}"
            )

        wybin_poll = _parse_int(
            environ.get("WYBIN_POLL_MINUTES", ""),
            DEFAULT_WYBIN_POLL_MINUTES,
            MIN_WYBIN_POLL_MINUTES,
        )
        if wybin_poll is None:
            invalid["WYBIN_POLL_MINUTES"] = f"must be a number of at least {MIN_WYBIN_POLL_MINUTES}"

        wybin_interval = _parse_int(
            environ.get("WYBIN_MIN_INTERVAL_SECONDS", ""),
            DEFAULT_WYBIN_MIN_INTERVAL_SECONDS,
            1,
            MAX_WYBIN_MIN_INTERVAL_SECONDS,
        )
        if wybin_interval is None:
            invalid["WYBIN_MIN_INTERVAL_SECONDS"] = (
                f"must be a number from 1 to {MAX_WYBIN_MIN_INTERVAL_SECONDS}"
            )

        capture_hours = _parse_int(
            environ.get("CAPTURE_REFRESH_HOURS", ""), DEFAULT_CAPTURE_REFRESH_HOURS, 1
        )
        if capture_hours is None:
            invalid["CAPTURE_REFRESH_HOURS"] = "must be a number of at least 1"

        live_poll = _parse_int(
            environ.get("LIVE_POLL_SECONDS", ""), DEFAULT_LIVE_POLL_SECONDS, MIN_LIVE_POLL_SECONDS
        )
        if live_poll is None:
            invalid["LIVE_POLL_SECONDS"] = f"must be a number of at least {MIN_LIVE_POLL_SECONDS}"

        scanner = _scanner_settings(environ, osu_api_rate, invalid)
        ai = _ai_settings(environ, invalid)

        if missing or invalid:
            raise ConfigError(missing, invalid)

        return cls(
            osu_client_id=values["OSU_CLIENT_ID"],
            osu_client_secret=values["OSU_CLIENT_SECRET"],
            public_url=public_url.rstrip("/"),
            session_secret=session_secret,
            admin_osu_ids=admin_ids,
            data_dir=Path(environ.get("DATA_DIR", "").strip() or DEFAULT_DATA_DIR),
            log_level=log_level,
            osu_api_rate_per_min=osu_api_rate,
            wybin_poll_minutes=wybin_poll,
            wybin_min_interval_seconds=wybin_interval,
            capture_refresh_hours=capture_hours,
            live_poll_seconds=live_poll,
            **scanner,
            **ai,
        )


def _scanner_settings(
    environ: Mapping[str, str], osu_api_rate: int | None, invalid: dict[str, str]
) -> dict[str, Any]:
    """The SCANNER_* variables as `Settings` keyword arguments; problems are added to `invalid`.

    The scanner's own request budget must stay below the overall osu! budget, or it could use up
    everything the live and interactive calls need. When it is not set, it defaults to 30 or, if
    the overall budget is lower, to one request less than that budget."""
    enabled = _parse_bool(environ.get("SCANNER_ENABLED", ""), True)
    if enabled is None:
        invalid["SCANNER_ENABLED"] = "must be true or false"
    default_budget = DEFAULT_SCANNER_BUDGET_PER_MIN
    if osu_api_rate is not None:
        default_budget = max(1, min(default_budget, osu_api_rate - 1))
    budget = _parse_int(environ.get("SCANNER_BUDGET_PER_MIN", ""), default_budget, 1)
    if budget is None:
        invalid["SCANNER_BUDGET_PER_MIN"] = "must be a number of at least 1"
    elif enabled and osu_api_rate is not None and budget >= osu_api_rate:
        invalid["SCANNER_BUDGET_PER_MIN"] = "must be lower than OSU_API_RATE_PER_MIN"
    workers = _parse_int(
        environ.get("SCANNER_WORKERS", ""),
        DEFAULT_SCANNER_WORKERS,
        MIN_SCANNER_WORKERS,
        MAX_SCANNER_WORKERS,
    )
    if workers is None:
        invalid["SCANNER_WORKERS"] = (
            f"must be a number from {MIN_SCANNER_WORKERS} to {MAX_SCANNER_WORKERS}"
        )
    backlog = _parse_int(environ.get("SCANNER_MAX_BACKLOG", ""), DEFAULT_SCANNER_MAX_BACKLOG, 1)
    if backlog is None:
        invalid["SCANNER_MAX_BACKLOG"] = "must be a number of at least 1"
    open_hours = _parse_int(
        environ.get("SCANNER_OPEN_MAX_HOURS", ""), DEFAULT_SCANNER_OPEN_MAX_HOURS, 1
    )
    if open_hours is None:
        invalid["SCANNER_OPEN_MAX_HOURS"] = "must be a number of at least 1"
    return {
        "scanner_enabled": enabled,
        "scanner_budget_per_min": budget,
        "scanner_workers": workers,
        "scanner_max_backlog": backlog,
        "scanner_open_max_hours": open_hours,
    }


def _ai_settings(environ: Mapping[str, str], invalid: dict[str, str]) -> dict[str, Any]:
    """The AI_* variables as `Settings` keyword arguments; problems are added to `invalid`.

    A provider without a model or key is not an error: AI predictions just stay off (see
    `Settings.ai_missing`)."""
    provider = environ.get("AI_PROVIDER", "").strip().lower()
    if provider and provider not in AI_PROVIDERS:
        invalid["AI_PROVIDER"] = "must be empty or one of " + ", ".join(AI_PROVIDERS)
    model = environ.get("AI_MODEL", "").strip()
    if len(model) > MAX_AI_MODEL_LENGTH or any(c.isspace() or not c.isprintable() for c in model):
        invalid["AI_MODEL"] = f"must be a model name of at most {MAX_AI_MODEL_LENGTH} characters"
    base_url = environ.get("AI_BASE_URL", "").strip().rstrip("/")
    if base_url and not _is_api_base_url(base_url):
        invalid["AI_BASE_URL"] = "must look like https://host[:port][/path] without credentials"
    return {
        "ai_provider": provider,
        "ai_api_key": environ.get("AI_API_KEY", "").strip(),
        "ai_model": model,
        "ai_base_url": base_url,
    }


def _is_api_base_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
    )


def _parse_bool(raw: str, default: bool) -> bool | None:
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE_WORDS:
        return True
    return False if value in _FALSE_WORDS else None


def _is_origin(url: str) -> bool:
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


def _parse_admin_ids(raw: str) -> frozenset[int] | None:
    """Comma-separated positive integers; blanks between commas are ignored."""
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items or not all(item.isascii() and item.isdecimal() for item in items):
        return None
    ids = frozenset(int(item) for item in items)
    return None if 0 in ids else ids


def _parse_rate(raw: str) -> int | None:
    """Empty means the default (the maximum); otherwise a whole number from 1 to the maximum."""
    value = raw.strip()
    if not value:
        return MAX_OSU_API_RATE_PER_MIN
    if not (value.isascii() and value.isdecimal()):
        return None
    rate = int(value)
    return rate if 1 <= rate <= MAX_OSU_API_RATE_PER_MIN else None


def _parse_int(raw: str, default: int, minimum: int, maximum: int | None = None) -> int | None:
    """Empty means the default; otherwise a whole number of at least `minimum` (and at most
    `maximum` when given)."""
    value = raw.strip()
    if not value:
        return default
    if not (value.isascii() and value.isdecimal()):
        return None
    number = int(value)
    if number < minimum or (maximum is not None and number > maximum):
        return None
    return number
