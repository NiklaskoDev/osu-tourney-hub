"""Settings read from environment variables.

Errors name the variables that are wrong and never echo their values.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
        )


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
