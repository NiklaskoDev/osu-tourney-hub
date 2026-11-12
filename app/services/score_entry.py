"""Hand-entered scores and notes on pool maps: validation and the rules for who may change what.

Raises `EntryError` for a refused form; `field` names the input to mark and `status` is the HTTP
status the web layer answers with.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import PoolMap, Score, Tournament
from app.domain.modes import canonical_mods
from app.domain.parsing import parse_uint
from app.repos import members as members_repo
from app.repos import notes as notes_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.services.links import source_link

MAX_SCORE = 2_000_000_000
MAX_COUNT = 1_000_000  # misses and combo: far above anything a map has
MAX_NOTE_LENGTH = 2000
NOTE_VISIBILITIES = ("private", "team")
_ACCURACY = re.compile(r"(\d{1,3})(?:[.,](\d{1,4}))?%?")
_SCORE_SEPARATORS = str.maketrans("", "", ", '_")


class EntryError(Exception):
    def __init__(self, message: str, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


@dataclass(frozen=True)
class Actor:
    osu_id: int
    can_manage: bool  # captain and above


@dataclass(frozen=True)
class ScoreForm:
    pool_map_id: str
    osu_id: str
    score: str
    accuracy: str
    misses: str = ""
    max_combo: str = ""
    mods: list[str] = field(default_factory=list)
    link: str = ""


def record_score(
    conn: sqlite3.Connection,
    tournament: Tournament,
    actor: Actor,
    form: ScoreForm,
    now: datetime,
) -> int:
    """Validate a hand-entered score and store it in the tournament's scoring type."""
    pool_map = tournament_map(conn, tournament.id, form.pool_map_id)
    if pool_map.beatmap_id is None:
        raise EntryError("This map has no osu! beatmap, so scores cannot be added.", 422)
    player = _player(conn, tournament.id, actor, form.osu_id)
    new = scores_repo.NewScore(
        beatmap_id=pool_map.beatmap_id,
        osu_id=player,
        gamemode=tournament.gamemode,
        mods=_mods(pool_map, form.mods, tournament.gamemode),
        score=_score(form.score),
        scoring=tournament.scoring,
        accuracy=_accuracy(form.accuracy),
        max_combo=_optional_count(form.max_combo, "max_combo", "Combo"),
        misses=_optional_count(form.misses, "misses", "Misses"),
        source_url=_link(form.link),
        entered_by=actor.osu_id,
        tournament_id=tournament.id,
    )
    return scores_repo.insert_manual(conn, new, to_utc_iso(now))


def delete_score(
    conn: sqlite3.Connection, tournament: Tournament, actor: Actor, score_id: int
) -> Score:
    """Remove a hand-entered score: its player may remove it; captains may remove the ones
    typed into this tournament for a member. Captured scores are public data and stay."""
    score = scores_repo.get(conn, score_id)
    if score is None:
        raise EntryError("This score does not exist.", 404)
    if score.source not in ("manual", "import"):
        raise EntryError("Only scores added by hand can be removed.", 409)
    own = score.osu_id == actor.osu_id
    managed = (
        actor.can_manage
        and score.tournament_id == tournament.id
        and members_repo.get(conn, tournament.id, score.osu_id) is not None
    )
    if not own and not managed:
        raise EntryError("You cannot remove this score.", 403)
    scores_repo.delete(conn, score_id)
    return score


def add_note(
    conn: sqlite3.Connection,
    tournament: Tournament,
    actor: Actor,
    pool_map_id: str,
    visibility: str,
    body: str,
    now: datetime,
) -> int:
    pool_map = tournament_map(conn, tournament.id, pool_map_id)
    text = body.strip()
    if not text or len(text) > MAX_NOTE_LENGTH:
        raise EntryError(f"Write a note of 1 to {MAX_NOTE_LENGTH} characters.", 422, "body")
    if visibility not in NOTE_VISIBILITIES:
        raise EntryError("Choose who can read the note.", 422, "visibility")
    with transaction(conn):
        return notes_repo.insert(
            conn, tournament.id, pool_map.id, actor.osu_id, visibility, text, to_utc_iso(now)
        )


def delete_note(
    conn: sqlite3.Connection, tournament: Tournament, actor: Actor, note_id: int
) -> None:
    """Only the author removes a note, whatever their role."""
    note = notes_repo.get(conn, note_id)
    if note is None or note.tournament_id != tournament.id:
        raise EntryError("This note does not exist.", 404)
    if note.author_osu_id != actor.osu_id:
        raise EntryError("You can only remove your own notes.", 403)
    notes_repo.delete(conn, note_id)


def tournament_map(conn: sqlite3.Connection, tournament_id: int, raw_id: str) -> PoolMap:
    """The pool map with this id if it belongs to the tournament, else a 404."""
    map_id = parse_uint(raw_id)
    pool_map = stages_repo.get_map(conn, map_id) if map_id is not None else None
    stage = stages_repo.get(conn, pool_map.stage_id) if pool_map else None
    if pool_map is None or stage is None or stage.tournament_id != tournament_id:
        raise EntryError("This map is not in this tournament.", 404)
    return pool_map


def _player(conn: sqlite3.Connection, tournament_id: int, actor: Actor, raw_id: str) -> int:
    """Players enter their own scores; captains enter them for any player on the roster."""
    osu_id = parse_uint(raw_id) if raw_id.strip() else actor.osu_id
    if osu_id is not None and osu_id != actor.osu_id and not actor.can_manage:
        raise EntryError("You can only add your own scores.", 403)
    member = members_repo.get(conn, tournament_id, osu_id) if osu_id is not None else None
    if member is None or member.role == "guest":
        raise EntryError("Choose a player from the roster.", 422, "osu_id")
    return member.osu_id


def _score(text: str) -> int:
    value = parse_uint(text.translate(_SCORE_SEPARATORS))
    if value is None or value > MAX_SCORE:
        raise EntryError(
            f"Enter the score as a whole number from 0 to {MAX_SCORE:,}.", 422, "score"
        )
    return value


def _accuracy(text: str) -> float:
    found = _ACCURACY.fullmatch(text.strip())
    value = float(f"{found.group(1)}.{found.group(2) or 0}") if found else None
    if value is None or value > 100:
        raise EntryError(
            "Enter the accuracy as a number from 0 to 100, for example 97.85.", 422, "accuracy"
        )
    return value


def _optional_count(text: str, field_name: str, label: str) -> int | None:
    if not text.strip():
        return None
    value = parse_uint(text)
    if value is None or value > MAX_COUNT:
        raise EntryError(
            f"{label} must be a whole number from 0 to {MAX_COUNT:,}.", 422, field_name
        )
    return value


def _mods(pool_map: PoolMap, chosen: list[str], gamemode: int) -> str:
    """Freemod and tiebreaker maps take the mods that were ticked; every other map is played with
    its required mods."""
    if not (pool_map.is_freemod or pool_map.is_tiebreaker):
        return pool_map.required_mods
    try:
        return canonical_mods(chosen, gamemode)
    except ValueError as error:
        raise EntryError(str(error) + ".", 422, "mods") from error


def _link(text: str) -> str | None:
    if not text.strip():
        return None
    link = source_link(text)
    if link is None:
        raise EntryError("Paste an osu! score or multiplayer match link.", 422, "link")
    return link
