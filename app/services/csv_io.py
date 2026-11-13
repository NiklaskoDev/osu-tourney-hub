"""Sheet export and score import as CSV.

Export: the pool sheet (every player's best score per map) and the saved lineup of a stage.
Import: a table of scores (from a file or a Google Sheet) is checked row by row against the
tournament's roster and pools into an `ImportPlan`; `commit_import` stores the plan's scores. The
check is repeated on every step, so nothing from the preview page is trusted.
"""

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import Member, PoolMap, Stage, Tournament
from app.domain.modes import canonical_mods, display_mods, split_mods
from app.domain.parsing import parse_uint
from app.repos import lineups as lineups_repo
from app.repos import members as members_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.services import pool_sheet, sheet
from app.services.csv_text import CsvTable, clean_cell, guard_cell
from app.services.score_entry import (
    EntryError,
    parse_accuracy,
    parse_optional_count,
    parse_score,
)

POOL_HEADER = (
    "slot",
    "beatmap_id",
    "player",
    "osu_id",
    "score",
    "accuracy",
    "misses",
    "mods",
    "source",
)
LINEUP_HEADER = ("slot", "beatmap_id", "player", "osu_id", "mods", "status")

# What each import field is called in a file, most specific first. Headers are compared in lower
# case with "_" and "-" read as spaces.
FIELD_LABELS = {
    "slot": "Map slot",
    "beatmap": "Beatmap id",
    "player": "Player name",
    "osu_id": "Player osu! id",
    "score": "Score",
    "accuracy": "Accuracy",
    "misses": "Misses",
    "mods": "Mods",
}
_ALIASES = {
    "slot": ("slot", "map slot", "pool slot"),
    "beatmap": ("beatmap id", "beatmap", "map id", "map"),
    "player": ("player", "player name", "username", "user name", "user", "name"),
    "osu_id": ("osu id", "player id", "user id", "id"),
    "score": ("score", "total score"),
    "accuracy": ("accuracy", "acc", "accuracy %", "acc %"),
    "misses": ("misses", "miss", "count miss", "countmiss"),
    "mods": ("mods", "mod"),
}
MAPPING_PREFIX = "map_"
MAP_FIELDS = ("slot", "beatmap")
PLAYER_FIELDS = ("player", "osu_id")
REQUIRED_FIELDS = ("score", "accuracy")
_MOD_SEPARATORS = re.compile(r"[\s,+/]")  # "HD, HR" and "HD+HR" both mean HDHR

Mapping = dict[str, int | None]  # import field -> column index in the file


# --- export ----------------------------------------------------------------------------------


def export_pool(conn: sqlite3.Connection, tournament: Tournament, stage: Stage) -> str:
    """One row per map and player who has a counting score on it: their best."""
    data = pool_sheet.load_stage(conn, tournament, stage)
    rows = []
    for pool_map in data.pool:
        for player in data.players:
            found = data.best.get((player.osu_id, pool_map.id))
            if found is None:
                continue
            best = found.overall
            rows.append(
                (
                    pool_map.slot,
                    pool_map.beatmap_id,
                    _name(player),
                    player.osu_id,
                    best.score,
                    f"{best.accuracy:.2f}",
                    best.misses,
                    display_mods(best.mods),
                    best.source,
                )
            )
    return _csv(POOL_HEADER, rows)


def export_lineup(conn: sqlite3.Connection, tournament: Tournament, stage: Stage) -> str:
    """The stage's saved lineup, one row per map and player. Header only when none is saved."""
    saved = lineups_repo.get_for_stage(conn, stage.id)
    entries = lineups_repo.list_entries(conn, saved.id) if saved else []
    maps = {pool_map.id: pool_map for pool_map in stages_repo.list_maps(conn, stage.id)}
    names = {m.osu_id: _name(m) for m in members_repo.list_for_tournament(conn, tournament.id)}
    rows = [
        (
            maps[entry.pool_map_id].slot,
            maps[entry.pool_map_id].beatmap_id,
            names.get(entry.osu_id, f"osu! user {entry.osu_id}"),
            entry.osu_id,
            display_mods(entry.mods),
            saved.status if saved else "",
        )
        for entry in sorted(entries, key=lambda e: maps[e.pool_map_id].sort_order)
        if entry.pool_map_id in maps
    ]
    return _csv(LINEUP_HEADER, rows)


def _csv(header: tuple[str, ...], rows: list[tuple]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(
            "" if cell is None else guard_cell(cell) if isinstance(cell, str) else cell
            for cell in row
        )
    return out.getvalue()


def _name(member: Member) -> str:
    return member.username or f"osu! user {member.osu_id}"


# --- import: mapping the columns -------------------------------------------------------------


def detect_mapping(headers: list[str]) -> Mapping:
    """Guess which column holds each import field from the header names."""
    normal = [_normal(header) for header in headers]
    mapping: Mapping = {}
    for name, aliases in _ALIASES.items():
        mapping[name] = next((normal.index(a) for a in aliases if a in normal), None)
    return mapping


def mapping_from_form(values: dict[str, str], column_count: int) -> Mapping:
    """The mapping a person chose on the preview page; anything unusable counts as "not in the
    file"."""
    mapping: Mapping = {}
    for name in FIELD_LABELS:
        index = parse_uint(values.get(MAPPING_PREFIX + name, ""))
        mapping[name] = index if index is not None and index < column_count else None
    return mapping


def mapping_problems(mapping: Mapping) -> list[str]:
    """What is still missing before rows can be read."""
    problems = []
    if all(mapping.get(name) is None for name in MAP_FIELDS):
        problems.append("Choose the column with the map slot or the beatmap id.")
    if all(mapping.get(name) is None for name in PLAYER_FIELDS):
        problems.append("Choose the column with the player name or osu! id.")
    for name in REQUIRED_FIELDS:
        if mapping.get(name) is None:
            problems.append(f"Choose the column with the {FIELD_LABELS[name].lower()}.")
    return problems


def _normal(header: str) -> str:
    return " ".join(header.lower().replace("_", " ").replace("-", " ").split())


# --- import: checking the rows ---------------------------------------------------------------


@dataclass(frozen=True)
class PlannedScore:
    line: int
    osu_id: int
    player: str
    pool_map: PoolMap
    beatmap_id: int
    mods: str
    score: int
    accuracy: float
    misses: int | None


@dataclass(frozen=True)
class RowProblem:
    line: int
    message: str


@dataclass
class ImportPlan:
    total: int
    scores: list[PlannedScore] = field(default_factory=list)
    problems: list[RowProblem] = field(default_factory=list)
    unknown_players: list[str] = field(default_factory=list)
    duplicates: int = 0


class _RowError(Exception):
    """One row cannot be imported; the message says why."""


class _UnknownPlayer(Exception):
    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


@dataclass(frozen=True)
class _Lookup:
    """What a row is checked against: the roster, and the pool maps by slot and beatmap."""

    stage: Stage
    gamemode: int
    by_id: dict[int, Member]
    by_name: dict[str, Member]
    slots: dict[str, PoolMap]
    beatmaps: dict[int, PoolMap]


def plan_import(
    conn: sqlite3.Connection,
    tournament: Tournament,
    stage: Stage,
    table: CsvTable,
    mapping: Mapping,
) -> ImportPlan:
    """Check every row of the table. Rows that are fine and new go to `scores`; the rest are
    counted as duplicates, listed as problems, or (players not on the roster) listed by name."""
    lookup = _lookup(conn, tournament, stage)
    plan = ImportPlan(total=len(table.rows))
    unknown: dict[str, None] = {}
    parsed: list[PlannedScore] = []
    for line, row in zip(table.lines, table.rows, strict=True):
        try:
            parsed.append(_read_row(line, row, mapping, lookup))
        except _RowError as problem:
            plan.problems.append(RowProblem(line, str(problem)))
        except _UnknownPlayer as player:
            unknown[player.label] = None
            plan.problems.append(RowProblem(line, f"{player.label} is not on the roster."))
    plan.unknown_players = list(unknown)
    known = _existing_scores(conn, tournament, parsed)
    for planned in parsed:
        key = (planned.osu_id, planned.beatmap_id, planned.mods, planned.score)
        if key in known:
            plan.duplicates += 1
        else:
            known.add(key)
            plan.scores.append(planned)
    return plan


def commit_import(
    conn: sqlite3.Connection,
    tournament: Tournament,
    plan: ImportPlan,
    entered_by: int,
    now: datetime,
) -> int:
    """Store the plan's scores in one transaction; returns how many."""
    with transaction(conn):
        for planned in plan.scores:
            scores_repo.insert_imported(
                conn,
                scores_repo.NewScore(
                    beatmap_id=planned.beatmap_id,
                    osu_id=planned.osu_id,
                    gamemode=tournament.gamemode,
                    mods=planned.mods,
                    score=planned.score,
                    scoring=tournament.scoring,
                    accuracy=planned.accuracy,
                    max_combo=None,
                    misses=planned.misses,
                    source_url=None,
                    entered_by=entered_by,
                    tournament_id=tournament.id,
                ),
                to_utc_iso(now),
            )
    return len(plan.scores)


def _lookup(conn: sqlite3.Connection, tournament: Tournament, stage: Stage) -> _Lookup:
    players = [
        m for m in members_repo.list_for_tournament(conn, tournament.id) if m.role != "guest"
    ]
    beatmaps: dict[int, PoolMap] = {}
    # The chosen stage is read last so its map wins when a beatmap is in two pools.
    others = [s for s in stages_repo.list_for_tournament(conn, tournament.id) if s.id != stage.id]
    for each in (*others, stage):
        for pool_map in stages_repo.list_maps(conn, each.id):
            if pool_map.beatmap_id is not None:
                beatmaps[pool_map.beatmap_id] = pool_map
    return _Lookup(
        stage=stage,
        gamemode=tournament.gamemode,
        by_id={m.osu_id: m for m in players},
        by_name={m.username.casefold(): m for m in players if m.username},
        slots={m.slot.upper(): m for m in stages_repo.list_maps(conn, stage.id)},
        beatmaps=beatmaps,
    )


def _read_row(line: int, row: list[str], mapping: Mapping, lookup: _Lookup) -> PlannedScore:
    def cell(name: str) -> str:
        index = mapping.get(name)
        return clean_cell(row[index]) if index is not None and index < len(row) else ""

    pool_map, beatmap_id = _resolve_map(cell("slot"), cell("beatmap"), lookup)
    member = _resolve_player(cell("osu_id"), cell("player"), lookup)
    try:
        score = parse_score(cell("score"))
        accuracy = parse_accuracy(cell("accuracy"))
        misses = parse_optional_count(cell("misses"), "misses", "Misses")
    except EntryError as problem:
        raise _RowError(problem.message) from problem
    mods = _resolve_mods(pool_map, cell("mods"), lookup.gamemode)
    return PlannedScore(
        line=line,
        osu_id=member.osu_id,
        player=member.username or f"osu! user {member.osu_id}",
        pool_map=pool_map,
        beatmap_id=beatmap_id,
        mods=mods,
        score=score,
        accuracy=accuracy,
        misses=misses,
    )


def _resolve_map(slot: str, beatmap: str, lookup: _Lookup) -> tuple[PoolMap, int]:
    by_slot = by_beatmap = None
    if slot:
        by_slot = lookup.slots.get(slot.upper())
        if by_slot is None:
            raise _RowError(f"There is no map {slot} in {lookup.stage.name}.")
    if beatmap:
        beatmap_id = parse_uint(beatmap)
        by_beatmap = lookup.beatmaps.get(beatmap_id) if beatmap_id is not None else None
        if by_beatmap is None:
            raise _RowError(f"Beatmap {beatmap} is not in this tournament's pools.")
    found = by_slot or by_beatmap
    if found is None:
        raise _RowError("The row names no map.")
    if by_slot and by_beatmap and by_slot.id != by_beatmap.id:
        raise _RowError(f"Map {slot} and beatmap {beatmap} are not the same map.")
    if found.beatmap_id is None:
        raise _RowError(f"{found.slot} has no osu! beatmap yet.")
    return found, found.beatmap_id


def _resolve_player(osu_id: str, name: str, lookup: _Lookup) -> Member:
    if osu_id:
        number = parse_uint(osu_id)
        member = lookup.by_id.get(number) if number is not None else None
        if member is None:
            raise _UnknownPlayer(osu_id)
        return member
    if not name:
        raise _RowError("The row names no player.")
    member = lookup.by_name.get(name.casefold())
    if member is None:
        raise _UnknownPlayer(name)
    return member


def _resolve_mods(pool_map: PoolMap, text: str, gamemode: int) -> str:
    """Freemod and tiebreaker maps take the mods in the file; any other map is played with its
    required mods, so the file may leave them out or repeat them."""
    try:
        mods = canonical_mods(split_mods(_MOD_SEPARATORS.sub("", text)), gamemode)
    except ValueError as error:
        raise _RowError(f"{error}.") from error
    if pool_map.is_freemod or pool_map.is_tiebreaker:
        return mods
    if text and not sheet.accepts_mods(pool_map, mods):
        raise _RowError(f"Mods {display_mods(mods)} do not match {pool_map.slot}.")
    return pool_map.required_mods


def _existing_scores(
    conn: sqlite3.Connection, tournament: Tournament, planned: list[PlannedScore]
) -> set[tuple[int, int, str, int]]:
    beatmaps = {p.beatmap_id for p in planned}
    players = {p.osu_id for p in planned}
    stored = scores_repo.list_for_beatmaps(conn, beatmaps, players, tournament.gamemode)
    return {(s.osu_id, s.beatmap_id, s.mods, s.score) for s in stored}
