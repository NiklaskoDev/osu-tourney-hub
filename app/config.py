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
TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("", "0", "false", "no", "off")


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
    # Phase 0 only: serves /styleguide before login exists. Replaced by the admin check in phase 1.
    styleguide_preview: bool

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

        styleguide_preview = _parse_flag(environ.get("STYLEGUIDE_PREVIEW", ""))
        if styleguide_preview is None:
            invalid["STYLEGUIDE_PREVIEW"] = "must be true or false"

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
            styleguide_preview=bool(styleguide_preview),
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


def _parse_flag(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return None
