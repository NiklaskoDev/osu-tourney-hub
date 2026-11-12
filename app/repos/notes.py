"""SQL for notes on pool maps. A private note is only ever read back by its author."""

import sqlite3

from app.domain.models import Note

_SELECT = (
    "SELECT id, tournament_id, pool_map_id, author_osu_id, visibility, body, updated_at FROM notes "
)


def get(conn: sqlite3.Connection, note_id: int) -> Note | None:
    row = conn.execute(_SELECT + "WHERE id = ?", (note_id,)).fetchone()
    return Note(**row) if row else None


def list_visible(
    conn: sqlite3.Connection, tournament_id: int, pool_map_ids: list[int], viewer_osu_id: int
) -> list[Note]:
    """Team notes and the viewer's own private notes on these maps, oldest first."""
    if not pool_map_ids:
        return []
    marks = ", ".join("?" for _ in pool_map_ids)
    rows = conn.execute(
        _SELECT  # noqa: S608 - only placeholders are built dynamically
        + f"WHERE tournament_id = ? AND pool_map_id IN ({marks}) "
        "AND (visibility = 'team' OR author_osu_id = ?) ORDER BY id",
        [tournament_id, *pool_map_ids, viewer_osu_id],
    ).fetchall()
    return [Note(**row) for row in rows]


def insert(
    conn: sqlite3.Connection,
    tournament_id: int,
    pool_map_id: int,
    author_osu_id: int,
    visibility: str,
    body: str,
    updated_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO notes (tournament_id, pool_map_id, author_osu_id, visibility, body, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tournament_id, pool_map_id, author_osu_id, visibility, body, updated_at),
    )
    return int(cursor.lastrowid)


def delete(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
