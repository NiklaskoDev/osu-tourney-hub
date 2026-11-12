"""What the app keeps from wybin.xyz responses, and the parsers that read them.

wybin has no documented API. Field names below were confirmed on 2026-11-12 against real
responses of two team tournaments (osu!catch, 3 to 6 players per team) with at most five
anonymous requests; the raw responses were not stored.

`GET /api/v1/tournament/{slug}` returns one object:
  id, name, slug, acronym, gamemode (0-3), soloTournament, soloRegistration, minimumTeamSize,
  maximumTeamSize, teamConfiguration, ... plus three embedded lists:
  - stages[]: id, name, startDate and endDate (epoch ms, listed in no particular order), bestOf,
    bans, protects, qualifierStage, battleRoyale, modBrackets (null unless wybin embeds the pool)
  - teams[] (empty until teams exist): id, name, acronym, flagAcronym, qualifierSeed, timezones,
    teamMembers[player], creator (a player; the team's captain), pendingInvites[]
  - players[] (everyone signed up): id (the wybin player id), timezone ("UTC-6", "UTC+5:30" or
    null), timezoneIANA (or null), temporaryTeamCaptain, qualifierSeed, user{id, slug, username,
    userOsu{id, username, flag, avatar_url, ...}}. Discord ids also appear but are never read.
  modBrackets[]: id, name ("NoMod", "Hidden", "HardRock", "DoubleTime", "MixedMod",
    "Tiebreaker"), mods (text: "None", "Hidden", ...), beatmaps[]: beatmapId, beatmapsetId,
    beatmapOrder, name ("artist - title [difficulty]"), creator, mods (stable bitmask),
    starRating, bpm, totalLength, circleSize, approachRate, overallDifficulty, hpDrain,
    placeholder, customMap.
`GET /api/v1/tournament-matches/{id}` and `/tournament-teams/{id}` answer with the same wrapper
(matches under stages[].matches[], a plain team list); both were empty for the tournaments
checked, so neither is read yet.

Not confirmed with a real response (paths and shapes from the plan only; parsed tolerantly):
`GET /api/v1/tournament-mappools/{id}` (assumed to mirror the matches endpoint, with
stages[].modBrackets) and `GET /api/v1/users-detailed/{wybin user id}` (`participantTournaments`).

Parsers ignore unknown keys and fail only when something the app needs is missing or has the
wrong type: they raise `WybinFormatError`.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.clients.osu import safe_avatar
from app.clock import to_utc_iso
from app.domain.modes import is_valid_mode

MAX_TEXT_LENGTH = 200
_TIMEZONE_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_MAP_NAME = re.compile(r"^(?P<artist>.+?) - (?P<title>.+) \[(?P<version>[^\[\]]*)\]$", re.DOTALL)
MAX_OFFSET_MINUTES = 14 * 60


class WybinError(Exception):
    """Base class for everything the wybin client raises."""


class WybinNotFound(WybinError):
    """wybin has no such tournament or user."""


class WybinUnavailable(WybinError):
    """wybin did not give a usable answer, even after retries."""


class WybinFormatError(WybinError):
    """wybin answered, but not in the shape the parser knows."""


@dataclass(frozen=True)
class WybinPlayer:
    wybin_player_id: int
    wybin_user_id: int | None
    osu_id: int
    username: str
    avatar_url: str | None
    country: str | None
    timezone_iana: str | None
    timezone_offset_min: int | None
    is_temporary_captain: bool


@dataclass(frozen=True)
class WybinTeam:
    wybin_team_id: int
    name: str
    seed: int | None
    captain_player_id: int | None
    members: list[WybinPlayer]


@dataclass(frozen=True)
class WybinBeatmap:
    order: int
    beatmap_id: int | None
    beatmapset_id: int | None
    artist: str | None
    title: str | None
    version: str | None
    mapper: str | None
    mods_bitmask: int
    star_rating: float | None
    bpm: float | None
    length_s: int | None
    cs: float | None
    ar: float | None
    od: float | None
    hp: float | None
    is_placeholder: bool
    wybin_entry_id: int | None


@dataclass(frozen=True)
class WybinBracket:
    name: str
    mods: str | None
    beatmaps: list[WybinBeatmap]


@dataclass(frozen=True)
class WybinStage:
    wybin_stage_id: int
    name: str
    start_at: str | None
    end_at: str | None
    best_of: int | None
    bans: int | None
    protects: int | None
    is_qualifier: bool
    # None = wybin did not send a pool for this stage (unknown, not "empty").
    brackets: list[WybinBracket] | None


@dataclass(frozen=True)
class WybinTournament:
    wybin_id: int
    slug: str
    name: str
    acronym: str | None
    gamemode: int
    is_solo: bool
    team_size_min: int | None
    team_size_max: int | None
    stages: list[WybinStage]
    teams: list[WybinTeam]
    players: list[WybinPlayer]


@dataclass(frozen=True)
class WybinTournamentRef:
    """A tournament named by `participantTournaments`; either field may be missing."""

    wybin_id: int | None
    slug: str | None


@dataclass(frozen=True)
class WybinUserDetail:
    tournaments: list[WybinTournamentRef] = field(default_factory=list)


def parse_tournament(data: object) -> WybinTournament:
    obj = _object(data, "tournament")
    wybin_id = _require_int(obj, "id")
    gamemode = _require_int(obj, "gamemode", minimum=0)
    if not is_valid_mode(gamemode):
        raise WybinFormatError("tournament has an unknown game mode")
    team_size_min = _int_or_none(obj.get("minimumTeamSize"))
    team_size_max = _int_or_none(obj.get("maximumTeamSize"))
    is_solo = obj.get("soloTournament") is True or team_size_max == 1
    return WybinTournament(
        wybin_id=wybin_id,
        slug=_require_text(obj, "slug"),
        name=_require_text(obj, "name"),
        acronym=_text_or_none(obj.get("acronym")),
        gamemode=gamemode,
        is_solo=is_solo,
        team_size_min=team_size_min,
        team_size_max=team_size_max,
        stages=sorted(
            (_parse_stage(item) for item in _list(obj.get("stages"), "stages")),
            key=lambda stage: (stage.start_at is None, stage.start_at or "", stage.wybin_stage_id),
        ),
        teams=[_parse_team(item) for item in _list(obj.get("teams"), "teams")],
        players=_parse_players(_list(obj.get("players"), "players")),
    )


def parse_mappool_stages(data: object) -> dict[int, list[WybinBracket]]:
    """Pools by wybin stage id. Stages whose `modBrackets` is null are left out (unknown)."""
    obj = _object(data, "mappool")
    pools: dict[int, list[WybinBracket]] = {}
    for item in _list(obj.get("stages"), "stages"):
        stage = _object(item, "mappool stage")
        brackets = stage.get("modBrackets")
        if brackets is not None:
            pools[_require_int(stage, "id")] = _parse_brackets(brackets)
    return pools


def parse_user_detail(data: object) -> WybinUserDetail:
    obj = _object(data, "user")
    refs = []
    for item in _list(obj.get("participantTournaments"), "participantTournaments"):
        if not isinstance(item, dict):
            continue
        nested = item.get("tournament")
        entry = nested if isinstance(nested, dict) else item
        refs.append(
            WybinTournamentRef(_int_or_none(entry.get("id")), _text_or_none(entry.get("slug")))
        )
    return WybinUserDetail(refs)


def ms_to_utc_iso(value: object) -> str | None:
    """Epoch milliseconds -> the stored UTC text; None when missing or not a plausible date."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return to_utc_iso(datetime.fromtimestamp(value / 1000, UTC))
    except (OverflowError, OSError, ValueError):
        return None


def parse_timezone_offset(text: object) -> int | None:
    """ "UTC+5:30" -> 330, "UTC-6" -> -360, "UTC" -> 0. Anything else -> None."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value.upper() in ("UTC", "GMT", "Z"):
        return 0
    match = _TIMEZONE_OFFSET.match(value)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    minutes = sign * (int(match.group(2)) * 60 + int(match.group(3) or 0))
    return minutes if abs(minutes) <= MAX_OFFSET_MINUTES else None


def split_map_name(name: str | None) -> tuple[str | None, str | None, str | None]:
    """wybin sends one display name, "artist - title [difficulty]"; split it when it has that
    shape, otherwise the whole name becomes the title."""
    if not name:
        return None, None, None
    match = _MAP_NAME.match(name)
    if not match:
        return None, name[:MAX_TEXT_LENGTH], None
    return (
        match.group("artist")[:MAX_TEXT_LENGTH],
        match.group("title")[:MAX_TEXT_LENGTH],
        match.group("version")[:MAX_TEXT_LENGTH],
    )


def _parse_stage(item: object) -> WybinStage:
    obj = _object(item, "stage")
    brackets = obj.get("modBrackets")
    return WybinStage(
        wybin_stage_id=_require_int(obj, "id"),
        name=_require_text(obj, "name"),
        start_at=ms_to_utc_iso(obj.get("startDate")),
        end_at=ms_to_utc_iso(obj.get("endDate")),
        best_of=_int_or_none(obj.get("bestOf")),
        bans=_int_or_none(obj.get("bans")),
        protects=_int_or_none(obj.get("protects")),
        is_qualifier=obj.get("qualifierStage") is True,
        brackets=None if brackets is None else _parse_brackets(brackets),
    )


def _parse_brackets(data: object) -> list[WybinBracket]:
    brackets = []
    for item in _list(data, "modBrackets"):
        obj = _object(item, "mod bracket")
        brackets.append(
            WybinBracket(
                name=_require_text(obj, "name"),
                mods=_text_or_none(obj.get("mods")),
                beatmaps=[
                    _parse_beatmap(entry, index)
                    for index, entry in enumerate(_list(obj.get("beatmaps"), "beatmaps"), 1)
                ],
            )
        )
    return brackets


def _parse_beatmap(item: object, position: int) -> WybinBeatmap:
    obj = _object(item, "beatmap")
    artist, title, version = split_map_name(_text_or_none(obj.get("name")))
    return WybinBeatmap(
        order=_int_or_none(obj.get("beatmapOrder")) or position,
        beatmap_id=_int_or_none(obj.get("beatmapId")),
        beatmapset_id=_int_or_none(obj.get("beatmapsetId")),
        artist=artist,
        title=title,
        version=version,
        mapper=_text_or_none(obj.get("creator")),
        mods_bitmask=_int_or_none(obj.get("mods")) or 0,
        star_rating=_number_or_none(obj.get("starRating")),
        bpm=_number_or_none(obj.get("bpm")),
        length_s=_int_or_none(obj.get("totalLength")),
        cs=_number_or_none(obj.get("circleSize")),
        ar=_number_or_none(obj.get("approachRate")),
        od=_number_or_none(obj.get("overallDifficulty")),
        hp=_number_or_none(obj.get("hpDrain")),
        is_placeholder=obj.get("placeholder") is True or _int_or_none(obj.get("beatmapId")) is None,
        wybin_entry_id=_int_or_none(obj.get("id")),
    )


def _parse_team(item: object) -> WybinTeam:
    obj = _object(item, "team")
    creator = obj.get("creator")
    return WybinTeam(
        wybin_team_id=_require_int(obj, "id"),
        name=_require_text(obj, "name"),
        seed=_int_or_none(obj.get("qualifierSeed")),
        captain_player_id=_int_or_none(creator.get("id")) if isinstance(creator, dict) else None,
        members=_parse_players(_list(obj.get("teamMembers"), "teamMembers")),
    )


def _parse_players(items: list[Any]) -> list[WybinPlayer]:
    """One player without an osu! profile (a deleted account) is skipped; a list where none can
    be read means the shape changed, which must not look like an empty roster."""
    players = []
    for item in items:
        try:
            players.append(_parse_player(item))
        except WybinFormatError:
            continue
    if items and not players:
        raise WybinFormatError("no player could be read")
    return players


def _parse_player(item: object) -> WybinPlayer:
    obj = _object(item, "player")
    user = _object(obj.get("user"), "player user")
    osu = _object(user.get("userOsu"), "player osu! profile")
    flag = osu.get("flag")
    zone = obj.get("timezoneIANA")
    return WybinPlayer(
        wybin_player_id=_require_int(obj, "id"),
        wybin_user_id=_int_or_none(user.get("id")),
        osu_id=_require_int(osu, "id"),
        username=_require_text(osu, "username"),
        avatar_url=safe_avatar(osu.get("avatar_url")),
        country=flag if isinstance(flag, str) and len(flag) == 2 else None,
        timezone_iana=zone[:MAX_TEXT_LENGTH] if isinstance(zone, str) and zone else None,
        timezone_offset_min=parse_timezone_offset(obj.get("timezone")),
        is_temporary_captain=obj.get("temporaryTeamCaptain") is True,
    )


def _object(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WybinFormatError(f"{what} is not an object")
    return value


def _list(value: object, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WybinFormatError(f"{what} is not a list")
    return value


def _require_int(obj: dict[str, Any], key: str, minimum: int = 1) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WybinFormatError(f"{key} is missing or not a valid number")
    return value


def _require_text(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WybinFormatError(f"{key} is missing or empty")
    return value.strip()[:MAX_TEXT_LENGTH]


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_or_none(value: object) -> str | None:
    return value.strip()[:MAX_TEXT_LENGTH] if isinstance(value, str) and value.strip() else None


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
