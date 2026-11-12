"""Saving a stage's lineup: copying the suggestion, and the captain's edits.

Raises `LineupError` for a refused form; `field` names the input to mark.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import LineupEntry, PoolMap, Stage, Tournament
from app.domain.modes import canonical_mods, split_mods
from app.domain.parsing import parse_uint
from app.repos import lineups as lineups_repo
from app.repos import members as members_repo
from app.services.lineup import MapSuggestion
from app.services.tournament_settings import effective_lineup_size

LINEUP_STATUSES = ("draft", "final")
PICK_PREFIX = "pick_"
MODS_PREFIX = "mods_"
_MOD_SEPARATORS = re.compile(r"[\s,+]")  # "HD HR", "HD, HR" and "HD+HR" all mean HDHR


class LineupError(Exception):
    def __init__(self, message: str, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


@dataclass(frozen=True)
class LineupForm:
    status: str
    # Keyed by field name: `pick_<map id>_<slot>` holds an osu! id, `mods_<map id>_<slot>` mods.
    values: dict[str, str] = field(default_factory=dict)


def pick_field(pool_map_id: int, slot: int) -> str:
    return f"{PICK_PREFIX}{pool_map_id}_{slot}"


def mods_field(pool_map_id: int, slot: int) -> str:
    return f"{MODS_PREFIX}{pool_map_id}_{slot}"


def copy_suggestion(
    conn: sqlite3.Connection,
    tournament: Tournament,
    stage: Stage,
    suggestions: dict[int, MapSuggestion],
    updated_by: int,
    now: datetime,
) -> int:
    """Replace the stage's lineup with the suggestion, as a draft."""
    entries = [
        LineupEntry(pool_map_id, pick.osu_id, pick.mods)
        for pool_map_id, suggestion in suggestions.items()
        for pick in suggestion.picks
    ]
    with transaction(conn):
        return lineups_repo.save(
            conn, tournament.id, stage.id, "draft", entries, updated_by, to_utc_iso(now)
        )


def save_lineup(
    conn: sqlite3.Connection,
    tournament: Tournament,
    stage: Stage,
    pool: list[PoolMap],
    form: LineupForm,
    updated_by: int,
    now: datetime,
) -> int:
    """Validate the captain's edited lineup and store it as a draft or a final lineup."""
    if form.status not in LINEUP_STATUSES:
        raise LineupError("Choose draft or final.", 422, "status")
    entries = _entries(conn, tournament, stage, pool, form)
    with transaction(conn):
        return lineups_repo.save(
            conn, tournament.id, stage.id, form.status, entries, updated_by, to_utc_iso(now)
        )


def _entries(
    conn: sqlite3.Connection,
    tournament: Tournament,
    stage: Stage,
    pool: list[PoolMap],
    form: LineupForm,
) -> list[LineupEntry]:
    size = effective_lineup_size(stage, tournament)
    entries = []
    for pool_map in pool:
        chosen: set[int] = set()
        for slot in range(size):
            key = pick_field(pool_map.id, slot)
            text = form.values.get(key, "").strip()
            if not text:
                continue
            osu_id = _player(conn, tournament.id, text, key)
            if osu_id in chosen:
                raise LineupError("Choose each player once per map.", 422, key)
            chosen.add(osu_id)
            entries.append(
                LineupEntry(pool_map.id, osu_id, _mods(pool_map, form, slot, tournament.gamemode))
            )
    return entries


def _player(conn: sqlite3.Connection, tournament_id: int, text: str, key: str) -> int:
    osu_id = parse_uint(text)
    member = members_repo.get(conn, tournament_id, osu_id) if osu_id is not None else None
    if member is None or member.role == "guest":
        raise LineupError("Choose a player from the roster.", 422, key)
    return member.osu_id


def _mods(pool_map: PoolMap, form: LineupForm, slot: int, gamemode: int) -> str:
    """Freemod and tiebreaker slots carry the mods typed for them; every other map its own."""
    if not (pool_map.is_freemod or pool_map.is_tiebreaker):
        return pool_map.required_mods
    key = mods_field(pool_map.id, slot)
    try:
        return canonical_mods(
            split_mods(_MOD_SEPARATORS.sub("", form.values.get(key, ""))), gamemode
        )
    except ValueError as error:
        raise LineupError(f"{error}.", 422, key) from error
