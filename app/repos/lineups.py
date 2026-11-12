"""SQL for a stage's saved lineup and its entries."""

import sqlite3

from app.domain.models import Lineup, LineupEntry

_SELECT = (
    "SELECT id, tournament_id, stage_id, match_id, status, updated_by, updated_at FROM lineups "
)


def get_for_stage(conn: sqlite3.Connection, stage_id: int) -> Lineup | None:
    """The stage's lineup that is not tied to a match."""
    row = conn.execute(_SELECT + "WHERE stage_id = ? AND match_id IS NULL", (stage_id,)).fetchone()
    return Lineup(**row) if row else None


def list_entries(conn: sqlite3.Connection, lineup_id: int) -> list[LineupEntry]:
    rows = conn.execute(
        "SELECT pool_map_id, osu_id, mods FROM lineup_entries WHERE lineup_id = ? "
        "ORDER BY pool_map_id, rowid",
        (lineup_id,),
    ).fetchall()
    return [LineupEntry(**row) for row in rows]


def save(
    conn: sqlite3.Connection,
    tournament_id: int,
    stage_id: int,
    status: str,
    entries: list[LineupEntry],
    updated_by: int,
    updated_at: str,
) -> int:
    """Create or replace the stage's lineup with exactly `entries`; returns the lineup id."""
    conn.execute(
        "INSERT INTO lineups (tournament_id, stage_id, match_id, status, updated_by, updated_at) "
        "VALUES (?, ?, NULL, ?, ?, ?) ON CONFLICT (stage_id) WHERE match_id IS NULL DO UPDATE SET "
        "status = excluded.status, updated_by = excluded.updated_by, "
        "updated_at = excluded.updated_at",
        (tournament_id, stage_id, status, updated_by, updated_at),
    )
    row = conn.execute(
        "SELECT id FROM lineups WHERE stage_id = ? AND match_id IS NULL", (stage_id,)
    ).fetchone()
    lineup_id = int(row["id"])
    conn.execute("DELETE FROM lineup_entries WHERE lineup_id = ?", (lineup_id,))
    conn.executemany(
        "INSERT INTO lineup_entries (lineup_id, pool_map_id, osu_id, mods) VALUES (?, ?, ?, ?)",
        [(lineup_id, entry.pool_map_id, entry.osu_id, entry.mods) for entry in entries],
    )
    return lineup_id
