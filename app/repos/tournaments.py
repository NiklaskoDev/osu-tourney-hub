"""SQL for the tournaments table."""

import sqlite3
from dataclasses import dataclass

from app.domain.models import Tournament


@dataclass(frozen=True)
class TournamentEntry:
    """A tournament on someone's dashboard, with their stored role in it (None = not a member)."""

    tournament: Tournament
    member_role: str | None


def get(conn: sqlite3.Connection, tournament_id: int) -> Tournament | None:
    row = conn.execute(
        "SELECT id, name, acronym, status FROM tournaments WHERE id = ?", (tournament_id,)
    ).fetchone()
    return Tournament(**row) if row else None


def list_for_member(conn: sqlite3.Connection, osu_id: int) -> list[TournamentEntry]:
    """Tournaments the user belongs to, hidden ones left out. Archived ones come last."""
    rows = conn.execute(
        "SELECT t.id, t.name, t.acronym, t.status, m.role AS member_role "
        "FROM tournaments t JOIN tournament_members m ON m.tournament_id = t.id "
        "WHERE m.osu_id = ? AND t.status != 'hidden' "
        "ORDER BY t.status = 'archived', t.name COLLATE NOCASE, t.id",
        (osu_id,),
    ).fetchall()
    return [_entry(row) for row in rows]


def list_all(conn: sqlite3.Connection, osu_id: int) -> list[TournamentEntry]:
    """Every tournament, for admins; `member_role` is the admin's own role if they belong."""
    rows = conn.execute(
        "SELECT t.id, t.name, t.acronym, t.status, m.role AS member_role "
        "FROM tournaments t LEFT JOIN tournament_members m "
        "ON m.tournament_id = t.id AND m.osu_id = ? "
        "ORDER BY t.status = 'archived', t.name COLLATE NOCASE, t.id",
        (osu_id,),
    ).fetchall()
    return [_entry(row) for row in rows]


def _entry(row: sqlite3.Row) -> TournamentEntry:
    return TournamentEntry(
        Tournament(row["id"], row["name"], row["acronym"], row["status"]), row["member_role"]
    )
