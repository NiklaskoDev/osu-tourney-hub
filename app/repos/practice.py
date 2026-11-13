"""SQL for practice targets: a score and/or accuracy a player, or the whole team, should reach."""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    id: int
    tournament_id: int
    pool_map_id: int
    osu_id: int | None  # None = every player of the team
    target_score: int | None
    target_acc: float | None
    note: str | None
    set_by: int | None
    updated_at: str


_SELECT = (
    "SELECT id, tournament_id, pool_map_id, osu_id, target_score, target_acc, note, set_by, "
    "updated_at FROM practice_targets "
)


def list_for_maps(conn: sqlite3.Connection, pool_map_ids: Collection[int]) -> list[Target]:
    if not pool_map_ids:
        return []
    marks = ", ".join("?" for _ in pool_map_ids)
    rows = conn.execute(
        _SELECT  # noqa: S608 - only placeholders are built dynamically
        + f"WHERE pool_map_id IN ({marks}) ORDER BY pool_map_id, osu_id IS NOT NULL, osu_id",
        list(pool_map_ids),
    ).fetchall()
    return [Target(**row) for row in rows]


def upsert(
    conn: sqlite3.Connection,
    tournament_id: int,
    pool_map_id: int,
    osu_id: int | None,
    target_score: int | None,
    target_acc: float | None,
    note: str | None,
    set_by: int,
    now: str,
) -> None:
    """Set the target of a map for one player or, with `osu_id=None`, for the team. Setting it
    again replaces the earlier one."""
    existing = conn.execute(
        "SELECT id FROM practice_targets WHERE pool_map_id = ? AND osu_id IS ?",
        (pool_map_id, osu_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE practice_targets SET target_score = ?, target_acc = ?, note = ?, set_by = ?, "
            "updated_at = ? WHERE id = ?",
            (target_score, target_acc, note, set_by, now, existing["id"]),
        )
        return
    conn.execute(
        "INSERT INTO practice_targets (tournament_id, pool_map_id, osu_id, target_score, "
        "target_acc, note, set_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tournament_id, pool_map_id, osu_id, target_score, target_acc, note, set_by, now),
    )


def delete(conn: sqlite3.Connection, pool_map_id: int, osu_id: int | None) -> None:
    conn.execute(
        "DELETE FROM practice_targets WHERE pool_map_id = ? AND osu_id IS ?",
        (pool_map_id, osu_id),
    )
