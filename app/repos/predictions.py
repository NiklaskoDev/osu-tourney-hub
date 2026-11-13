"""SQL for the captain's plan of a match and for saved prediction snapshots."""

import sqlite3
from dataclasses import dataclass

PLAN_KINDS = ("protect", "ban", "pick")


@dataclass(frozen=True)
class PlanItem:
    kind: str  # one of PLAN_KINDS
    pool_map_id: int
    position: int  # 1-based, in the order the captain gave them
    note: str | None
    set_by: int | None = None


def list_plan(conn: sqlite3.Connection, match_id: int) -> list[PlanItem]:
    """The match's plan, protects first, then bans, then picks, each in order."""
    rows = conn.execute(
        "SELECT kind, pool_map_id, position, note, set_by FROM plan_items WHERE match_id = ? "
        "ORDER BY CASE kind WHEN 'protect' THEN 0 WHEN 'ban' THEN 1 ELSE 2 END, position",
        (match_id,),
    ).fetchall()
    return [PlanItem(**row) for row in rows]


def replace_plan(conn: sqlite3.Connection, match_id: int, items: list[PlanItem]) -> None:
    """Make the match's plan exactly `items`."""
    conn.execute("DELETE FROM plan_items WHERE match_id = ?", (match_id,))
    conn.executemany(
        "INSERT INTO plan_items (match_id, kind, pool_map_id, position, note, set_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (match_id, item.kind, item.pool_map_id, item.position, item.note, item.set_by)
            for item in items
        ],
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    match_id: int,
    kind: str,
    created_at: str,
    win_prob_team1: float,
    expected_score: str | None,
    payload: str,
    model_version: str,
) -> None:
    conn.execute(
        "INSERT INTO predictions (match_id, kind, created_at, win_prob_team1, expected_score, "
        "payload, model_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (match_id, kind, created_at, win_prob_team1, expected_score, payload, model_version),
    )


def has_snapshot_on_day(conn: sqlite3.Connection, match_id: int, kind: str, day: str) -> bool:
    """Whether a snapshot of this kind was saved on the UTC day `YYYY-MM-DD`."""
    row = conn.execute(
        "SELECT 1 FROM predictions WHERE match_id = ? AND kind = ? "
        "AND substr(created_at, 1, 10) = ? LIMIT 1",
        (match_id, kind, day),
    ).fetchone()
    return row is not None
