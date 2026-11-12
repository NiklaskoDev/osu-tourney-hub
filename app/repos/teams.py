"""SQL for teams and their players (ours and the opponents')."""

import sqlite3
from dataclasses import dataclass

from app.domain.models import Team, TeamPlayer

_SELECT_TEAM = "SELECT id, tournament_id, wybin_team_id, name, is_ours, seed FROM teams "


@dataclass(frozen=True)
class PlayerValues:
    osu_id: int
    username: str
    timezone_iana: str | None
    timezone_offset_min: int | None
    is_captain: bool


def get(conn: sqlite3.Connection, team_id: int) -> Team | None:
    row = conn.execute(_SELECT_TEAM + "WHERE id = ?", (team_id,)).fetchone()
    return _team(row) if row else None


def list_for_tournament(conn: sqlite3.Connection, tournament_id: int) -> list[Team]:
    rows = conn.execute(
        _SELECT_TEAM + "WHERE tournament_id = ? ORDER BY name COLLATE NOCASE, id",
        (tournament_id,),
    ).fetchall()
    return [_team(row) for row in rows]


def upsert_wybin_team(
    conn: sqlite3.Connection, tournament_id: int, wybin_team_id: int, name: str, seed: int | None
) -> int:
    """Create or refresh a team known by its wybin id; returns the team id."""
    conn.execute(
        "INSERT INTO teams (tournament_id, wybin_team_id, name, seed) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (tournament_id, wybin_team_id) DO UPDATE SET "
        "name = excluded.name, seed = excluded.seed",
        (tournament_id, wybin_team_id, name, seed),
    )
    row = conn.execute(
        "SELECT id FROM teams WHERE tournament_id = ? AND wybin_team_id = ?",
        (tournament_id, wybin_team_id),
    ).fetchone()
    return int(row["id"])


def insert_local_team(conn: sqlite3.Connection, tournament_id: int, name: str) -> int:
    """A team that does not come from wybin (manual tournaments)."""
    cursor = conn.execute(
        "INSERT INTO teams (tournament_id, name) VALUES (?, ?)", (tournament_id, name)
    )
    return int(cursor.lastrowid)


def replace_players(conn: sqlite3.Connection, team_id: int, players: list[PlayerValues]) -> None:
    """Make the team's players exactly `players`."""
    for player in players:
        conn.execute(
            "INSERT INTO team_players (team_id, osu_id, username, timezone_iana, "
            "timezone_offset_min, is_captain) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (team_id, osu_id) DO UPDATE SET username = excluded.username, "
            "timezone_iana = excluded.timezone_iana, "
            "timezone_offset_min = excluded.timezone_offset_min, is_captain = excluded.is_captain",
            (
                team_id,
                player.osu_id,
                player.username,
                player.timezone_iana,
                player.timezone_offset_min,
                int(player.is_captain),
            ),
        )
    keep = [player.osu_id for player in players]
    placeholders = ", ".join("?" for _ in keep) or "NULL"
    conn.execute(
        f"DELETE FROM team_players WHERE team_id = ? AND osu_id NOT IN ({placeholders})",  # noqa: S608
        [team_id, *keep],
    )


def list_players(conn: sqlite3.Connection, team_id: int) -> list[TeamPlayer]:
    rows = conn.execute(
        "SELECT team_id, osu_id, username, timezone_iana, timezone_offset_min, is_captain "
        "FROM team_players WHERE team_id = ? ORDER BY is_captain DESC, username COLLATE NOCASE",
        (team_id,),
    ).fetchall()
    return [_player(row) for row in rows]


def find_team_with_player(
    conn: sqlite3.Connection, tournament_id: int, osu_ids: list[int]
) -> int | None:
    """The team containing the first of `osu_ids` that is on any team (earlier ids win)."""
    for osu_id in osu_ids:
        row = conn.execute(
            "SELECT t.id FROM teams t JOIN team_players p ON p.team_id = t.id "
            "WHERE t.tournament_id = ? AND p.osu_id = ? ORDER BY t.id LIMIT 1",
            (tournament_id, osu_id),
        ).fetchone()
        if row:
            return int(row["id"])
    return None


def _team(row: sqlite3.Row) -> Team:
    values = dict(row)
    values["is_ours"] = bool(values["is_ours"])
    return Team(**values)


def _player(row: sqlite3.Row) -> TeamPlayer:
    values = dict(row)
    values["is_captain"] = bool(values["is_captain"])
    return TeamPlayer(**values)
