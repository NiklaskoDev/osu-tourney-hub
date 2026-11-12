"""IANA timezone names the profile page offers and accepts."""

import zoneinfo

_REGIONS = frozenset(
    {
        "Africa",
        "America",
        "Antarctica",
        "Arctic",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
    }
)


def timezone_names() -> list[str]:
    """Region zones plus UTC, sorted. Legacy aliases like US/Eastern are left out."""
    names = {
        name
        for name in zoneinfo.available_timezones()
        if name.partition("/")[0] in _REGIONS and "/" in name
    }
    return ["UTC", *sorted(names)]


def is_valid_timezone(name: str) -> bool:
    return name in _VALID


_VALID = frozenset(timezone_names())
