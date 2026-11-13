"""SQL for the matches table."""

import sqlite3
from dataclasses import dataclass

from app.domain.models import Match

_SELECT = (
    "SELECT id, tournament_id, stage_id, wybin_match_id, label, team1_id, team2_id, scheduled_at, "
    "score1, score2, mp_match_id, status, updated_at FROM matches "
)


@dataclass(frozen=True)
class MatchValues:
    """What a wybin sync knows about a match."""

    wybin_match_id: int
    stage_id: int | None
    label: str | None
    team1_id: int | None
    team2_id: int | None
    scheduled_at: str | None
    score1: int | None
    score2: int | None
    mp_match_id: int | None
    status: str


def get(conn: sqlite3.Connection, match_id: int) -> Match | None:
    row = conn.execute(_SELECT + "WHERE id = ?", (match_id,)).fetchone()
    return Match(**row) if row else None


def list_for_tournament(conn: sqlite3.Connection, tournament_id: int) -> list[Match]:
    """Matches in time order; the ones without a time come last."""
    rows = conn.execute(
        _SELECT + "WHERE tournament_id = ? ORDER BY scheduled_at IS NULL, scheduled_at, id",
        (tournament_id,),
    ).fetchall()
    return [Match(**row) for row in rows]


def upsert_wybin(
    conn: sqlite3.Connection, tournament_id: int, values: MatchValues, now: str
) -> None:
    """Create or refresh a match known by its wybin id. A match id that already belongs to
    another tournament is left alone."""
    conn.execute(
        "INSERT INTO matches (tournament_id, stage_id, wybin_match_id, label, team1_id, team2_id, "
        "scheduled_at, score1, score2, mp_match_id, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (wybin_match_id) DO UPDATE SET stage_id = excluded.stage_id, "
        "label = excluded.label, team1_id = excluded.team1_id, team2_id = excluded.team2_id, "
        "scheduled_at = excluded.scheduled_at, score1 = excluded.score1, "
        "score2 = excluded.score2, mp_match_id = excluded.mp_match_id, "
        "status = excluded.status, updated_at = excluded.updated_at "
        "WHERE matches.tournament_id = excluded.tournament_id",
        (
            tournament_id,
            values.stage_id,
            values.wybin_match_id,
            values.label,
            values.team1_id,
            values.team2_id,
            values.scheduled_at,
            values.score1,
            values.score2,
            values.mp_match_id,
            values.status,
            now,
        ),
    )


def list_for_calendar(conn: sqlite3.Connection, osu_id: int) -> list[Match]:
    """Matches with a time, of our team, in every visible tournament the person belongs to."""
    rows = conn.execute(
        "SELECT m.id, m.tournament_id, m.stage_id, m.wybin_match_id, m.label, m.team1_id, "
        "m.team2_id, m.scheduled_at, m.score1, m.score2, m.mp_match_id, m.status, m.updated_at "
        "FROM matches m "
        "JOIN tournaments t ON t.id = m.tournament_id "
        "JOIN tournament_members tm ON tm.tournament_id = t.id AND tm.osu_id = ? "
        "WHERE t.status = 'active' AND m.scheduled_at IS NOT NULL "
        "AND t.our_team_id IN (m.team1_id, m.team2_id) "
        "ORDER BY m.scheduled_at, m.id",
        (osu_id,),
    ).fetchall()
    return [Match(**row) for row in rows]
