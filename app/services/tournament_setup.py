"""Tournaments, stages and pool maps entered by hand (the fallback when wybin has no data).

Rules live here; the routes only turn a `SetupError` into a page. Nothing here is awaited while a
transaction is open: the osu! lookup for a map happens before the write.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.clients.osu import OsuClient, OsuError, OsuNotFound
from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import BeatmapMeta, Tournament, User
from app.domain.modes import (
    bracket_for_prefix,
    is_freemod_bracket,
    is_tiebreaker_bracket,
    is_valid_mode,
    required_mods_for_prefix,
)
from app.domain.parsing import parse_uint
from app.repos import members as members_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo
from app.repos.teams import PlayerValues
from app.services.links import map_id

MAX_NAME_LENGTH = 100
MAX_ACRONYM_LENGTH = 16
MAX_STAGE_NAME_LENGTH = 60
MAX_TEAM_SIZE = 16
MAX_BEST_OF = 21
MAX_BANS_OR_PROTECTS = 5
OUR_TEAM_NAME = "Our team"
_SLOT = re.compile(r"^([A-Z]{2,8})(\d{1,2})?$")
_FORM_TIME = "%Y-%m-%dT%H:%M"
ARCHIVED_MESSAGE = "This tournament is archived. Restore it first."
UNKNOWN_BEATMAP = "No beatmap with this id exists on osu!."
OSU_LOOKUP_FAILED = "osu! did not answer, so the map could not be looked up. Try again later."


class SetupError(Exception):
    """A refused form. `field` names the input to mark (None = a message above the form)."""

    def __init__(self, message: str, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


@dataclass(frozen=True)
class TournamentForm:
    name: str
    acronym: str
    gamemode: str
    is_solo: bool
    team_size_min: str
    team_size_max: str


@dataclass(frozen=True)
class StageForm:
    name: str
    start_at: str
    end_at: str
    best_of: str
    bans: str
    protects: str
    is_qualifier: bool


def create_manual(
    conn: sqlite3.Connection, owner: User, form: TournamentForm, now: datetime
) -> int:
    """Create a tournament with our team and the creator as its first captain."""
    name = _required_text(form.name, MAX_NAME_LENGTH, "name", "Enter a name for the tournament.")
    acronym = form.acronym.strip()
    if len(acronym) > MAX_ACRONYM_LENGTH:
        raise SetupError(
            f"Keep the acronym under {MAX_ACRONYM_LENGTH} characters.", field="acronym"
        )
    gamemode = _optional_int(form.gamemode, 0, 3, "gamemode", "The game mode")
    if gamemode is None or not is_valid_mode(gamemode):
        raise SetupError("Choose a game mode.", field="gamemode")
    size_min, size_max = (1, 1) if form.is_solo else _team_sizes(form)
    stamp = to_utc_iso(now)
    with transaction(conn):
        tournament_id = tournaments_repo.insert(
            conn,
            {
                "name": name,
                "acronym": acronym or None,
                "gamemode": gamemode,
                "is_solo": int(form.is_solo),
                "team_size_min": size_min,
                "team_size_max": size_max,
                "owner_osu_id": owner.osu_id,
                "source": "manual",
            },
            stamp,
        )
        team_id = teams_repo.insert_local_team(
            conn, tournament_id, owner.username if form.is_solo else OUR_TEAM_NAME
        )
        teams_repo.replace_players(
            conn, team_id, [PlayerValues(owner.osu_id, owner.username, owner.timezone, None, True)]
        )
        tournaments_repo.set_our_team(conn, tournament_id, team_id)
        members_repo.add(conn, tournament_id, owner.osu_id, "captain", "manual", owner.osu_id)
    return tournament_id


def add_stage(conn: sqlite3.Connection, tournament: Tournament, form: StageForm) -> int:
    _require_editable(tournament)
    name = _required_text(
        form.name,
        MAX_STAGE_NAME_LENGTH,
        "name",
        "Enter a name for the stage, for example Round of 16.",
    )
    start_at = _form_time(form.start_at, "start_at")
    end_at = _form_time(form.end_at, "end_at")
    if start_at and end_at and end_at < start_at:
        raise SetupError("The stage cannot end before it starts.", field="end_at")
    best_of = _optional_int(form.best_of, 1, MAX_BEST_OF, "best_of", "Best of")
    if best_of is not None and best_of % 2 == 0:
        raise SetupError("Best of must be an odd number, for example 9.", field="best_of")
    bans = _optional_int(form.bans, 0, MAX_BANS_OR_PROTECTS, "bans", "Bans")
    protects = _optional_int(form.protects, 0, MAX_BANS_OR_PROTECTS, "protects", "Protects")
    with transaction(conn):
        return stages_repo.insert(
            conn,
            tournament.id,
            {
                "name": name,
                "sort_order": stages_repo.next_sort_order(conn, tournament.id),
                "start_at": start_at,
                "end_at": end_at,
                "best_of": best_of,
                "bans": bans,
                "protects": protects,
                "is_qualifier": int(form.is_qualifier),
            },
        )


def remove_stage(conn: sqlite3.Connection, tournament: Tournament, stage_id: int) -> None:
    _require_editable(tournament)
    with transaction(conn):
        _require_stage(conn, tournament, stage_id)
        stages_repo.delete(conn, stage_id)


async def add_pool_map(
    conn: sqlite3.Connection,
    osu: OsuClient,
    tournament: Tournament,
    stage_id: int,
    slot_text: str,
    beatmap_text: str,
) -> int:
    """Add one map to a stage. The slot prefix decides the required mods (HD2 needs HD, NM1 none)
    and the beatmap's details are looked up on osu!."""
    _require_editable(tournament)
    _require_stage(conn, tournament, stage_id)
    slot, prefix, required_mods = _parse_slot(slot_text, tournament.gamemode)
    beatmap_id = map_id(beatmap_text)
    if beatmap_id is None or not 0 < beatmap_id < 2**31:
        raise SetupError("Enter a beatmap link or id, for example 129891.", field="beatmap")
    meta = await _lookup(osu, beatmap_id)
    bracket = bracket_for_prefix(prefix)
    row = {
        "bracket": bracket,
        "required_mods": required_mods,
        "is_freemod": int(is_freemod_bracket(bracket, None)),
        "is_tiebreaker": int(is_tiebreaker_bracket(bracket, None)),
        "beatmap_id": meta.beatmap_id,
        "beatmapset_id": meta.beatmapset_id,
        "artist": meta.artist,
        "title": meta.title,
        "version": meta.version,
        "mapper": meta.mapper,
        "star_rating": meta.star_rating,
        "bpm": meta.bpm,
        "length_s": meta.length_s,
        "cs": meta.cs,
        "ar": meta.ar,
        "od": meta.od,
        "hp": meta.hp,
    }
    with transaction(conn):
        row["sort_order"] = stages_repo.next_map_sort_order(conn, stage_id)
        try:
            map_row_id = stages_repo.insert_map(conn, stage_id, slot, row)
        except sqlite3.IntegrityError as error:
            raise SetupError(
                f"The slot {slot} is already used in this stage.", 409, "slot"
            ) from error
        stages_repo.set_pool_released(conn, stage_id, True)
    return map_row_id


def remove_pool_map(conn: sqlite3.Connection, tournament: Tournament, map_row_id: int) -> None:
    _require_editable(tournament)
    with transaction(conn):
        pool_map = stages_repo.get_map(conn, map_row_id)
        if pool_map is None:
            raise SetupError("This map is not in the pool.", 404)
        _require_stage(conn, tournament, pool_map.stage_id)
        stages_repo.delete_map(conn, map_row_id)
        stages_repo.set_pool_released(
            conn, pool_map.stage_id, stages_repo.count_maps(conn, pool_map.stage_id) > 0
        )


def _require_editable(tournament: Tournament) -> None:
    if tournament.source != "manual":
        raise SetupError("Stages and maps of a wybin tournament come from wybin.", 409)
    if tournament.status == "archived":
        raise SetupError(ARCHIVED_MESSAGE, 409)


def _require_stage(conn: sqlite3.Connection, tournament: Tournament, stage_id: int) -> None:
    stage = stages_repo.get(conn, stage_id)
    if stage is None or stage.tournament_id != tournament.id:
        raise SetupError("This stage does not exist.", 404)


def _parse_slot(text: str, gamemode: int) -> tuple[str, str, str]:
    """(slot, prefix, required mods) for "hd2"; raises `SetupError` for an unusable slot."""
    slot = text.strip().upper()
    match = _SLOT.match(slot)
    if not match:
        raise SetupError("Use a slot like NM1, HD2, FM1 or TB.", field="slot")
    prefix = match.group(1)
    try:
        return slot, prefix, required_mods_for_prefix(prefix, gamemode)
    except ValueError as error:
        raise SetupError(
            f"{prefix} is not a mod combination this game mode has.", field="slot"
        ) from error


async def _lookup(osu: OsuClient, beatmap_id: int) -> BeatmapMeta:
    try:
        return await osu.get_beatmap(beatmap_id)
    except OsuNotFound as error:
        raise SetupError(UNKNOWN_BEATMAP, field="beatmap") from error
    except OsuError as error:
        raise SetupError(OSU_LOOKUP_FAILED, 502) from error


def _team_sizes(form: TournamentForm) -> tuple[int | None, int | None]:
    low = _optional_int(form.team_size_min, 1, MAX_TEAM_SIZE, "team_size_min", "The smallest team")
    high = _optional_int(form.team_size_max, 1, MAX_TEAM_SIZE, "team_size_max", "The largest team")
    if low is not None and high is not None and low > high:
        raise SetupError(
            "The smallest team cannot be bigger than the largest.", field="team_size_min"
        )
    return low, high


def _required_text(value: str, limit: int, field: str, empty_message: str) -> str:
    text = value.strip()
    if not text:
        raise SetupError(empty_message, field=field)
    if len(text) > limit:
        raise SetupError(f"Keep this under {limit} characters.", field=field)
    return text


def _optional_int(value: str, low: int, high: int, field: str, label: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    number = parse_uint(text)
    if number is None or not low <= number <= high:
        raise SetupError(f"{label} must be a whole number from {low} to {high}.", field=field)
    return number


def _form_time(value: str, field: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    try:
        return to_utc_iso(datetime.strptime(text, _FORM_TIME).replace(tzinfo=UTC))
    except ValueError as error:
        raise SetupError("Enter a date and time.", field=field) from error
