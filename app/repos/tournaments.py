"""SQL for the tournaments table."""

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.domain.models import Tournament

_SELECT = "SELECT t.* FROM tournaments t "
_SELECT_WITH_ROLE = "SELECT t.*, m.role AS member_role FROM tournaments t "
_FIELDS = tuple(Tournament.__dataclass_fields__)


@dataclass(frozen=True)
class TournamentEntry:
    """A tournament on someone's dashboard, with their stored role in it (None = not a member)."""

    tournament: Tournament
    member_role: str | None


def get(conn: sqlite3.Connection, tournament_id: int) -> Tournament | None:
    row = conn.execute(_SELECT + "WHERE t.id = ?", (tournament_id,)).fetchone()
    return _tournament(row) if row else None


def find_by_wybin_id(conn: sqlite3.Connection, wybin_id: int) -> Tournament | None:
    row = conn.execute(_SELECT + "WHERE t.wybin_id = ?", (wybin_id,)).fetchone()
    return _tournament(row) if row else None


def find_by_slug(conn: sqlite3.Connection, slug: str) -> Tournament | None:
    row = conn.execute(_SELECT + "WHERE t.slug = ?", (slug,)).fetchone()
    return _tournament(row) if row else None


def list_syncable(conn: sqlite3.Connection) -> list[Tournament]:
    """Active tournaments that came from wybin: the ones the poller keeps up to date."""
    rows = conn.execute(
        _SELECT + "WHERE t.status = 'active' AND t.source = 'wybin' AND t.slug IS NOT NULL "
        "ORDER BY t.id"
    ).fetchall()
    return [_tournament(row) for row in rows]


def list_for_member(conn: sqlite3.Connection, osu_id: int) -> list[TournamentEntry]:
    """Tournaments the user belongs to, hidden ones left out. Archived ones come last."""
    rows = conn.execute(
        _SELECT_WITH_ROLE + "JOIN tournament_members m ON m.tournament_id = t.id "
        "WHERE m.osu_id = ? AND t.status != 'hidden' "
        "ORDER BY t.status = 'archived', t.name COLLATE NOCASE, t.id",
        (osu_id,),
    ).fetchall()
    return [_entry(row) for row in rows]


def list_all(conn: sqlite3.Connection, osu_id: int) -> list[TournamentEntry]:
    """Every tournament, for admins; `member_role` is the admin's own role if they belong."""
    rows = conn.execute(
        _SELECT_WITH_ROLE + "LEFT JOIN tournament_members m "
        "ON m.tournament_id = t.id AND m.osu_id = ? "
        "ORDER BY t.status = 'archived', t.name COLLATE NOCASE, t.id",
        (osu_id,),
    ).fetchall()
    return [_entry(row) for row in rows]


def insert(conn: sqlite3.Connection, values: dict[str, Any], created_at: str) -> int:
    """Insert a tournament from column values (all names fixed by the caller, never by input)."""
    columns = [*values, "created_at"]
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO tournaments ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
        [*values.values(), created_at],
    )
    return int(cursor.lastrowid)


def update_from_wybin(
    conn: sqlite3.Connection, tournament_id: int, values: dict[str, Any], synced_at: str
) -> None:
    """Refresh the fields wybin owns; the app's own settings (rules, status, placing) stay."""
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(
        f"UPDATE tournaments SET {assignments}, last_synced_at = ? WHERE id = ?",  # noqa: S608
        [*values.values(), synced_at, tournament_id],
    )


def set_status(
    conn: sqlite3.Connection, tournament_id: int, status: str, archived_at: str | None
) -> None:
    conn.execute(
        "UPDATE tournaments SET status = ?, archived_at = ? WHERE id = ?",
        (status, archived_at, tournament_id),
    )


def set_rules(
    conn: sqlite3.Connection, tournament_id: int, freemod_rule: str, scoring: str
) -> None:
    conn.execute(
        "UPDATE tournaments SET freemod_rule = ?, scoring = ? WHERE id = ?",
        (freemod_rule, scoring, tournament_id),
    )


def set_final_placing(conn: sqlite3.Connection, tournament_id: int, placing: str | None) -> None:
    conn.execute("UPDATE tournaments SET final_placing = ? WHERE id = ?", (placing, tournament_id))


def set_our_team(conn: sqlite3.Connection, tournament_id: int, team_id: int | None) -> None:
    conn.execute("UPDATE tournaments SET our_team_id = ? WHERE id = ?", (team_id, tournament_id))
    conn.execute(
        "UPDATE teams SET is_ours = (id = ?) WHERE tournament_id = ?", (team_id, tournament_id)
    )


def delete(conn: sqlite3.Connection, tournament_id: int) -> None:
    conn.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))


def _tournament(row: sqlite3.Row) -> Tournament:
    values = {field: row[field] for field in _FIELDS}
    values["is_solo"] = bool(values["is_solo"])
    return Tournament(**values)


def _entry(row: sqlite3.Row) -> TournamentEntry:
    return TournamentEntry(_tournament(row), row["member_role"])
