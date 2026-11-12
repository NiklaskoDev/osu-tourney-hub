"""SQL for scores. A score is keyed by player and beatmap, so every tournament sees it."""

import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass

from app.domain.models import Score

_SELECT = (
    "SELECT id, beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, max_combo, misses, "
    "passed, achieved_at, source, source_ref, source_url, entered_by, tournament_id, created_at "
    "FROM scores "
)


@dataclass(frozen=True)
class NewScore:
    """A hand-entered score, already validated by `services.score_entry`."""

    beatmap_id: int
    osu_id: int
    gamemode: int
    mods: str
    score: int
    scoring: str
    accuracy: float
    max_combo: int | None
    misses: int | None
    source_url: str | None
    entered_by: int
    tournament_id: int


def insert_manual(conn: sqlite3.Connection, new: NewScore, created_at: str) -> int:
    """Store a hand-entered score. Every one gets its own reference, so none is a duplicate."""
    cursor = conn.execute(
        "INSERT INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, "
        "max_combo, misses, passed, source, source_ref, source_url, entered_by, tournament_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'manual', ?, ?, ?, ?, ?)",
        (
            new.beatmap_id,
            new.osu_id,
            new.gamemode,
            new.mods,
            new.score,
            new.scoring,
            new.accuracy,
            new.max_combo,
            new.misses,
            str(uuid.uuid4()),
            new.source_url,
            new.entered_by,
            new.tournament_id,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def get(conn: sqlite3.Connection, score_id: int) -> Score | None:
    row = conn.execute(_SELECT + "WHERE id = ?", (score_id,)).fetchone()
    return _score(row) if row else None


def list_for_beatmaps(
    conn: sqlite3.Connection, beatmap_ids: Collection[int], osu_ids: Collection[int], gamemode: int
) -> list[Score]:
    """Every score of these players on these beatmaps, whichever tournament it came from."""
    if not beatmap_ids or not osu_ids:
        return []
    beatmap_marks = ", ".join("?" for _ in beatmap_ids)
    player_marks = ", ".join("?" for _ in osu_ids)
    rows = conn.execute(
        _SELECT  # noqa: S608 - only placeholders are built dynamically
        + f"WHERE gamemode = ? AND beatmap_id IN ({beatmap_marks}) AND osu_id IN ({player_marks}) "
        "ORDER BY id",
        [gamemode, *beatmap_ids, *osu_ids],
    ).fetchall()
    return [_score(row) for row in rows]


def delete(conn: sqlite3.Connection, score_id: int) -> None:
    conn.execute("DELETE FROM scores WHERE id = ?", (score_id,))


def _score(row: sqlite3.Row) -> Score:
    values = dict(row)
    values["passed"] = bool(values["passed"])
    return Score(**values)
