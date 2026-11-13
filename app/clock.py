"""Time source. Code asks a Clock for "now" so tests can control it."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def to_utc_iso(moment: datetime) -> str:
    """Format as stored in the database: UTC, `YYYY-MM-DDTHH:MM:SSZ`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_or_none(value: object) -> str | None:
    """A timestamp from an API (ISO 8601, with or without an offset; no offset means UTC) in the
    database format, or None for anything that is not one."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return to_utc_iso(moment if moment.tzinfo else moment.replace(tzinfo=UTC))


def from_utc_iso(text: str) -> datetime:
    """Parse a database timestamp (`YYYY-MM-DDTHH:MM:SSZ`) into an aware UTC datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
