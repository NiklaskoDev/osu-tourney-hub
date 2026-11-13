"""SQL for match_events: the protects, bans and picks that were actually played in a match."""

import sqlite3
from dataclasses import dataclass

SOURCES = ("wybin", "mp", "manual")


@dataclass(frozen=True)
class MatchEvent:
    seq: int
    kind: str  # protect, ban, pick or tiebreaker
    team_side: int | None  # 1 or 2 as in the matches table; None when nobody could be named
    pool_map_id: int | None
    beatmap_id: int | None
    winner_side: int | None
    team1_score: int | None
    team2_score: int | None
    source: str  # one of SOURCES


@dataclass(frozen=True)
class TeamEvent:
    """One event seen from one team's seat, with what the habits need to know about its map."""

    match_id: int
    seq: int
    kind: str
    own: bool | None  # whether the team itself made it; None when the side is unknown
    pool_map_id: int | None
    bracket: str | None
    slot: str | None
    stage_id: int | None  # the stage of the match


_COLUMNS = (
    "seq, kind, team_side, pool_map_id, beatmap_id, winner_side, team1_score, team2_score, source"
)


def list_for_match(conn: sqlite3.Connection, match_id: int) -> list[MatchEvent]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM match_events WHERE match_id = ? ORDER BY seq",  # noqa: S608
        (match_id,),
    ).fetchall()
    return [MatchEvent(**row) for row in rows]


def replace_source(
    conn: sqlite3.Connection, match_id: int, source: str, events: list[MatchEvent]
) -> None:
    """Make the events of one source exactly `events`. A seq that a manual correction already
    holds keeps the correction."""
    conn.execute("DELETE FROM match_events WHERE match_id = ? AND source = ?", (match_id, source))
    taken = {
        row["seq"]
        for row in conn.execute("SELECT seq FROM match_events WHERE match_id = ?", (match_id,))
    }
    conn.executemany(
        f"INSERT INTO match_events (match_id, {_COLUMNS}) "  # noqa: S608
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [_row(match_id, event) for event in events if event.seq not in taken],
    )


def save_manual(conn: sqlite3.Connection, match_id: int, event: MatchEvent) -> None:
    """Store a correction, replacing whatever the match has at that seq."""
    conn.execute("DELETE FROM match_events WHERE match_id = ? AND seq = ?", (match_id, event.seq))
    conn.execute(
        f"INSERT INTO match_events (match_id, {_COLUMNS}) "  # noqa: S608
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _row(match_id, event),
    )


def delete_manual(conn: sqlite3.Connection, match_id: int, seq: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM match_events WHERE match_id = ? AND seq = ? AND source = 'manual'",
        (match_id, seq),
    )
    return cursor.rowcount > 0


def set_side(conn: sqlite3.Connection, match_id: int, seq: int, team_side: int | None) -> None:
    conn.execute(
        "UPDATE match_events SET team_side = ? WHERE match_id = ? AND seq = ?",
        (team_side, match_id, seq),
    )


def has_events(conn: sqlite3.Connection, match_id: int, source: str | None = None) -> bool:
    sql = "SELECT 1 FROM match_events WHERE match_id = ?"
    params: list[object] = [match_id]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def list_for_team(
    conn: sqlite3.Connection,
    tournament_id: int,
    team_id: int,
    exclude_match_id: int | None = None,
    before: str | None = None,
) -> list[TeamEvent]:
    """Every event of the team's matches in the tournament (the team's own and the other team's),
    in match and event order. `exclude_match_id` leaves one match out: the one being predicted.
    `before` (a UTC time) leaves out the matches scheduled at that time or later; a match
    without a time stays."""
    rows = conn.execute(
        "SELECT e.match_id, e.seq, e.kind, e.team_side, e.pool_map_id, p.bracket, p.slot, "
        "m.stage_id, m.team1_id "
        "FROM match_events e JOIN matches m ON m.id = e.match_id "
        "LEFT JOIN pool_maps p ON p.id = e.pool_map_id "
        "WHERE m.tournament_id = ? AND (m.team1_id = ? OR m.team2_id = ?) AND m.id != ? "
        "AND (? IS NULL OR m.scheduled_at IS NULL OR m.scheduled_at < ?) "
        "ORDER BY e.match_id, e.seq",
        (tournament_id, team_id, team_id, exclude_match_id or 0, before, before),
    ).fetchall()
    events = []
    for row in rows:
        own = None
        if row["team_side"] is not None:
            own = (row["team_side"] == 1) == (row["team1_id"] == team_id)
        events.append(
            TeamEvent(
                row["match_id"],
                row["seq"],
                row["kind"],
                own,
                row["pool_map_id"],
                row["bracket"],
                row["slot"],
                row["stage_id"],
            )
        )
    return events


def _row(match_id: int, event: MatchEvent) -> tuple:
    return (
        match_id,
        event.seq,
        event.kind,
        event.team_side,
        event.pool_map_id,
        event.beatmap_id,
        event.winner_side,
        event.team1_score,
        event.team2_score,
        event.source,
    )
