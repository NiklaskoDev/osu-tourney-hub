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


def from_utc_iso(text: str) -> datetime:
    """Parse a database timestamp (`YYYY-MM-DDTHH:MM:SSZ`) into an aware UTC datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
