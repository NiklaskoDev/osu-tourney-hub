"""SQL for tournament_members. Names come from the users table when the person has been seen."""

import sqlite3

from app.domain.models import Member

_SELECT = (
    "SELECT m.tournament_id, m.osu_id, u.username, u.avatar_url, m.role, m.is_substitute, "
    "m.temp_captain_until, m.source "
    "FROM tournament_members m LEFT JOIN users u ON u.osu_id = m.osu_id "
)


def get(conn: sqlite3.Connection, tournament_id: int, osu_id: int) -> Member | None:
    row = conn.execute(
        _SELECT + "WHERE m.tournament_id = ? AND m.osu_id = ?", (tournament_id, osu_id)
    ).fetchone()
    return _member(row) if row else None


def list_for_tournament(conn: sqlite3.Connection, tournament_id: int) -> list[Member]:
    """Captains first, then co-captains, players and guests; alphabetical inside each group."""
    rows = conn.execute(
        _SELECT + "WHERE m.tournament_id = ? "
        "ORDER BY CASE m.role WHEN 'captain' THEN 0 WHEN 'co_captain' THEN 1 "
        "WHEN 'player' THEN 2 ELSE 3 END, u.username COLLATE NOCASE, m.osu_id",
        (tournament_id,),
    ).fetchall()
    return [_member(row) for row in rows]


def count_with_role(conn: sqlite3.Connection, tournament_id: int, role: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tournament_members WHERE tournament_id = ? AND role = ?",
        (tournament_id, role),
    ).fetchone()
    return int(row["n"])


def add(
    conn: sqlite3.Connection,
    tournament_id: int,
    osu_id: int,
    role: str,
    source: str,
    added_by: int | None,
) -> None:
    """Insert a roster entry. Raises `sqlite3.IntegrityError` when the person is already in."""
    conn.execute(
        "INSERT INTO tournament_members (tournament_id, osu_id, role, source, added_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (tournament_id, osu_id, role, source, added_by),
    )


def update(
    conn: sqlite3.Connection,
    tournament_id: int,
    osu_id: int,
    role: str,
    is_substitute: bool,
    temp_captain_until: str | None,
) -> None:
    conn.execute(
        "UPDATE tournament_members SET role = ?, is_substitute = ?, temp_captain_until = ? "
        "WHERE tournament_id = ? AND osu_id = ?",
        (role, int(is_substitute), temp_captain_until, tournament_id, osu_id),
    )


def remove(conn: sqlite3.Connection, tournament_id: int, osu_id: int) -> None:
    conn.execute(
        "DELETE FROM tournament_members WHERE tournament_id = ? AND osu_id = ?",
        (tournament_id, osu_id),
    )


def _member(row: sqlite3.Row) -> Member:
    return Member(
        tournament_id=row["tournament_id"],
        osu_id=row["osu_id"],
        username=row["username"],
        avatar_url=row["avatar_url"],
        role=row["role"],
        is_substitute=bool(row["is_substitute"]),
        temp_captain_until=row["temp_captain_until"],
        source=row["source"],
    )
