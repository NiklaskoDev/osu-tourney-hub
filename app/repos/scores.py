"""SQL for scores. A score is keyed by player and beatmap, so every tournament sees it."""

import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass

from app.domain.models import ApiScore, MpScore, Score

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
    return _insert_hand_entered(conn, new, created_at, "manual")


def insert_imported(conn: sqlite3.Connection, new: NewScore, created_at: str) -> int:
    """Store a score from a CSV import. Callers skip duplicates before they get here."""
    return _insert_hand_entered(conn, new, created_at, "import")


def insert_api(conn: sqlite3.Connection, score: ApiScore, gamemode: int, created_at: str) -> bool:
    """Store a captured osu! score (always lazer standardised, D3; ScoreV2 tournaments count it too,
    marked). Returns False when this score id was captured before for that player."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, "
        "accuracy, max_combo, misses, passed, achieved_at, source, source_ref, source_url, "
        "created_at) VALUES (?, ?, ?, ?, ?, 'lazer', ?, ?, ?, 1, ?, 'api', ?, ?, ?)",
        (
            score.beatmap_id,
            score.osu_id,
            gamemode,
            score.mods,
            score.total_score,
            score.accuracy,
            score.max_combo,
            score.misses,
            score.achieved_at,
            str(score.score_id),
            f"https://osu.ppy.sh/scores/{score.score_id}",
            created_at,
        ),
    )
    return cursor.rowcount == 1


def insert_mp(
    conn: sqlite3.Connection,
    mp_match_id: int,
    game_id: int,
    beatmap_id: int,
    gamemode: int,
    scoring: str,
    achieved_at: str | None,
    score: MpScore,
    created_at: str,
) -> bool:
    """Store a score read from a multiplayer game, once per game and player. Returns False when
    it was stored before (a lobby read twice)."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, "
        "accuracy, max_combo, misses, passed, achieved_at, source, source_ref, source_url, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mp', ?, ?, ?)",
        (
            beatmap_id,
            score.osu_id,
            gamemode,
            score.mods,
            score.score,
            scoring,
            score.accuracy,
            score.max_combo,
            score.misses,
            int(score.passed),
            achieved_at,
            f"{mp_match_id}:{game_id}",
            f"https://osu.ppy.sh/community/matches/{mp_match_id}",
            created_at,
        ),
    )
    return cursor.rowcount == 1


def _insert_hand_entered(
    conn: sqlite3.Connection, new: NewScore, created_at: str, source: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, "
        "max_combo, misses, passed, source, source_ref, source_url, entered_by, tournament_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
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
            source,
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
