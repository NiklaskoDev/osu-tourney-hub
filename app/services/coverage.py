"""Practice coverage: who has a score on which map, whether the targets captains set are met, and
which maps of the likely lineup are not covered yet.

The likely lineup is the current lineup suggestion (substitutes left out). Raises `EntryError`
(from `score_entry`) for a refused target form.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import Member, PoolMap, Score, Tournament
from app.domain.parsing import parse_uint
from app.repos import members as members_repo
from app.repos import practice as practice_repo
from app.repos.practice import Target
from app.services import lineup, pool_sheet
from app.services.pool_sheet import StageData
from app.services.score_entry import EntryError, parse_accuracy, parse_score, tournament_map
from app.services.tournament_settings import effective_lineup_size

MAX_NOTE_LENGTH = 500


@dataclass(frozen=True)
class CoverageCell:
    score: Score | None
    in_likely_lineup: bool
    target: Target | None
    target_met: bool | None  # None when the player has no target on this map


@dataclass(frozen=True)
class CoverageRow:
    pool_map: PoolMap
    cells: list[CoverageCell]
    with_score: int  # members who have a score on the map
    flags: list[str]
    team_target: Target | None


@dataclass(frozen=True)
class CoverageView:
    columns: list[Member]
    rows: list[CoverageRow]
    lineup_size: int


@dataclass(frozen=True)
class TargetForm:
    pool_map_id: str
    osu_id: str  # empty = the whole team
    target_score: str
    target_acc: str
    note: str = ""


def target_met(target: Target, score: Score | None) -> bool:
    """A target is met when there is a score and it reaches every figure the target names."""
    if score is None:
        return False
    if target.target_score is not None and score.score < target.target_score:
        return False
    if target.target_acc is None:
        return True
    return score.accuracy is not None and score.accuracy >= target.target_acc


def build_coverage(data: StageData, tournament: Tournament, targets: list[Target]) -> CoverageView:
    """The coverage grid of one stage: a cell per member and map, and flags per map."""
    size = effective_lineup_size(data.stage, tournament)
    suggestions = pool_sheet.suggest_for_stage(data, tournament, include_substitutes=False)
    by_key = {(target.pool_map_id, target.osu_id): target for target in targets}
    rows = []
    for pool_map in data.pool:
        suggestion = suggestions[pool_map.id]
        likely = {pick.osu_id for pick in suggestion.picks}
        team_target = by_key.get((pool_map.id, None))
        cells = []
        for member in data.players:
            found = data.best.get((member.osu_id, pool_map.id))
            score = found.overall if found else None
            target = by_key.get((pool_map.id, member.osu_id)) or team_target
            cells.append(
                CoverageCell(
                    score,
                    member.osu_id in likely,
                    target,
                    target_met(target, score) if target else None,
                )
            )
        rows.append(
            CoverageRow(
                pool_map,
                cells,
                sum(1 for cell in cells if cell.score is not None),
                _flags(suggestion, size),
                team_target,
            )
        )
    return CoverageView(data.players, rows, size)


def _flags(suggestion: lineup.MapSuggestion, size: int) -> list[str]:
    if not suggestion.picks:
        return ["Nobody in the likely lineup has a score"]
    if suggestion.incomplete:
        return [f"Only {len(suggestion.picks)} of {size} have a score"]
    return []


def save_target(
    conn: sqlite3.Connection,
    tournament: Tournament,
    form: TargetForm,
    set_by: int,
    now: datetime,
) -> str:
    """Set, replace or (with no figure given) remove the target of a map. Returns "saved" or
    "removed"."""
    pool_map = tournament_map(conn, tournament.id, form.pool_map_id)
    osu_id = _target_player(conn, tournament.id, form.osu_id)
    score = _figure(parse_score, form.target_score, "target_score")
    accuracy = _figure(parse_accuracy, form.target_acc, "target_acc")
    note = form.note.strip() or None
    if note is not None and len(note) > MAX_NOTE_LENGTH:
        raise EntryError(f"The note can have at most {MAX_NOTE_LENGTH} characters.", 422, "note")
    with transaction(conn):
        if score is None and accuracy is None:
            practice_repo.delete(conn, pool_map.id, osu_id)
            return "removed"
        practice_repo.upsert(
            conn, tournament.id, pool_map.id, osu_id, score, accuracy, note, set_by, to_utc_iso(now)
        )
    return "saved"


def _figure[T](parse: Callable[[str], T], text: str, field_name: str) -> T | None:
    """A figure typed into the form, None when left empty; a refusal names this form's field."""
    if not text.strip():
        return None
    try:
        return parse(text)
    except EntryError as error:
        raise EntryError(error.message, error.status, field_name) from error


def _target_player(conn: sqlite3.Connection, tournament_id: int, raw: str) -> int | None:
    if not raw.strip():
        return None
    osu_id = parse_uint(raw)
    member = members_repo.get(conn, tournament_id, osu_id) if osu_id is not None else None
    if member is None or member.role == "guest":
        raise EntryError("Choose the whole team or a player from the roster.", 422, "osu_id")
    return member.osu_id
