"""Ids from osu! links, ported from the reference script (same behaviour)."""

import re

_MATCH_LINK = re.compile(r"matches/(\d+)")
_MAP_LINK = re.compile(r"(?:#\w+/|/b/|/beatmaps/)(\d+)")
_BARE_NUMBER = re.compile(r"\s*(\d+)\s*")


MAX_ID_DIGITS = 10  # longer numbers are no osu! id, and int() refuses thousands of digits


def _id_from(found: re.Match[str] | None) -> int | None:
    if found is None or len(found.group(1)) > MAX_ID_DIGITS:
        return None
    return int(found.group(1))


def match_id(link: str) -> int | None:
    """Multiplayer match id from a `.../matches/<id>` link or a bare number."""
    found = _MATCH_LINK.search(link) or _BARE_NUMBER.fullmatch(link)
    return _id_from(found)


def map_id(text: str) -> int | None:
    """Beatmap id from 129891, /b/129891, beatmapsets/X#osu/129891 or /beatmaps/129891."""
    found = _MAP_LINK.search(text) or _BARE_NUMBER.fullmatch(text)
    return _id_from(found)
