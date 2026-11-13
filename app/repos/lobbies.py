"""SQL for stored multiplayer lobbies, their pool-map hits and the registered players in them."""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Lobby:
    mp_match_id: int
    name: str | None
    started_at: str | None
    ended_at: str | None
    first_seen_at: str
    last_scanned_at: str | None
    is_complete: bool
    discovered_by: str


@dataclass(frozen=True)
class HitRow:
    mp_match_id: int
    pool_map_id: int
    slot: str
    stage_id: int
    sort_order: int


@dataclass(frozen=True)
class PlayerRow:
    mp_match_id: int
    osu_id: int
    team_id: int | None
    team_name: str | None
    username: str | None


@dataclass(frozen=True)
class WybinLink:
    tournament_id: int
    mp_match_id: int


_SELECT = (
    "SELECT mp_match_id, name, started_at, ended_at, first_seen_at, last_scanned_at, "
    "is_complete, discovered_by FROM lobbies "
)


def upsert(
    conn: sqlite3.Connection,
    mp_match_id: int,
    name: str,
    started_at: str | None,
    ended_at: str | None,
    now: str,
    complete: bool,
    discovered_by: str,
) -> None:
    """Store a lobby once. A later scan refreshes its name, end and completeness; `first_seen_at`
    and `discovered_by` stay as the first discovery set them, and a complete lobby stays so."""
    conn.execute(
        "INSERT INTO lobbies (mp_match_id, name, started_at, ended_at, first_seen_at, "
        "last_scanned_at, is_complete, discovered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (mp_match_id) DO UPDATE SET name = excluded.name, "
        "started_at = COALESCE(excluded.started_at, lobbies.started_at), "
        "ended_at = COALESCE(excluded.ended_at, lobbies.ended_at), "
        "last_scanned_at = excluded.last_scanned_at, "
        "is_complete = MAX(lobbies.is_complete, excluded.is_complete)",
        (mp_match_id, name, started_at, ended_at, now, now, int(complete), discovered_by),
    )


def refresh(
    conn: sqlite3.Connection,
    mp_match_id: int,
    name: str,
    ended_at: str | None,
    now: str,
    complete: bool,
) -> bool:
    """Bring an already stored lobby up to date after a read that found nothing new on pool maps
    (it may just have closed). Returns False when the lobby is not stored."""
    cursor = conn.execute(
        "UPDATE lobbies SET name = ?, ended_at = COALESCE(?, ended_at), last_scanned_at = ?, "
        "is_complete = MAX(is_complete, ?) WHERE mp_match_id = ?",
        (name, ended_at, now, int(complete), mp_match_id),
    )
    return cursor.rowcount == 1


def mark_complete(conn: sqlite3.Connection, mp_match_id: int, now: str) -> None:
    """A stored lobby that osu! no longer has can not change any more."""
    conn.execute(
        "UPDATE lobbies SET is_complete = 1, last_scanned_at = ? WHERE mp_match_id = ?",
        (now, mp_match_id),
    )


def insert_hit(
    conn: sqlite3.Connection,
    mp_match_id: int,
    game_id: int,
    pool_map_id: int,
    played_at: str | None,
    mods: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO lobby_pool_hits (mp_match_id, game_id, pool_map_id, played_at, "
        "mods) VALUES (?, ?, ?, ?, ?)",
        (mp_match_id, game_id, pool_map_id, played_at, mods),
    )


def insert_player(
    conn: sqlite3.Connection,
    mp_match_id: int,
    tournament_id: int,
    osu_id: int,
    team_id: int | None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO lobby_players (mp_match_id, tournament_id, osu_id, team_id) "
        "VALUES (?, ?, ?, ?)",
        (mp_match_id, tournament_id, osu_id, team_id),
    )


def get(conn: sqlite3.Connection, mp_match_id: int) -> Lobby | None:
    row = conn.execute(_SELECT + "WHERE mp_match_id = ?", (mp_match_id,)).fetchone()
    return _lobby(row) if row else None


def registered_players(
    conn: sqlite3.Connection, tournament_ids: Collection[int]
) -> dict[int, dict[int, int | None]]:
    """Per tournament: every registered player (on one of its teams or on its roster) and the
    team they play for, None for a roster member without a team."""
    found: dict[int, dict[int, int | None]] = {tid: {} for tid in tournament_ids}
    if not found:
        return found
    marks = ", ".join("?" for _ in found)
    members = conn.execute(
        f"SELECT tournament_id, osu_id FROM tournament_members WHERE tournament_id IN ({marks})",  # noqa: S608
        list(found),
    ).fetchall()
    for row in members:
        found[row["tournament_id"]][row["osu_id"]] = None
    team_players = conn.execute(
        "SELECT t.tournament_id, p.osu_id, p.team_id FROM team_players p "  # noqa: S608
        f"JOIN teams t ON t.id = p.team_id WHERE t.tournament_id IN ({marks}) ORDER BY p.team_id",
        list(found),
    ).fetchall()
    for row in team_players:
        found[row["tournament_id"]][row["osu_id"]] = row["team_id"]
    return found


def wybin_links_to_fetch(
    conn: sqlite3.Connection, limit: int, skip_ids: Collection[int] = ()
) -> list[WybinLink]:
    """Lobby links wybin gave for matches of active tournaments that are not stored yet, or are
    stored but were not finished when last read. One entry per lobby, the most recent match first
    (a link that never works then sinks to the end instead of blocking the others), leaving out
    `skip_ids`."""
    marks = ", ".join("?" for _ in skip_ids)
    skipped = f"AND m.mp_match_id NOT IN ({marks}) " if skip_ids else ""
    rows = conn.execute(
        "SELECT m.tournament_id, m.mp_match_id FROM matches m "  # noqa: S608
        "JOIN tournaments t ON t.id = m.tournament_id "
        "LEFT JOIN lobbies l ON l.mp_match_id = m.mp_match_id "
        "WHERE t.status = 'active' AND m.mp_match_id IS NOT NULL "
        f"AND (l.mp_match_id IS NULL OR l.is_complete = 0) {skipped}"
        "GROUP BY m.mp_match_id ORDER BY MAX(m.scheduled_at) DESC, m.mp_match_id DESC LIMIT ?",
        [*skip_ids, limit],
    ).fetchall()
    return [WybinLink(row["tournament_id"], row["mp_match_id"]) for row in rows]


def list_for_tournament(
    conn: sqlite3.Connection,
    tournament_id: int,
    stage_id: int | None,
    osu_id: int | None,
    limit: int,
) -> list[Lobby]:
    """Lobbies of a tournament, newest first: those with a hit on one of its pool maps and those
    registered players were found in. `stage_id` keeps lobbies with a hit in that stage; `osu_id`
    keeps lobbies that player was found in."""
    conditions = [
        "(l.mp_match_id IN (SELECT h.mp_match_id FROM lobby_pool_hits h "
        "JOIN pool_maps p ON p.id = h.pool_map_id JOIN stages s ON s.id = p.stage_id "
        "WHERE s.tournament_id = :tid) "
        "OR l.mp_match_id IN (SELECT mp_match_id FROM lobby_players WHERE tournament_id = :tid))"
    ]
    params: dict[str, int] = {"tid": tournament_id, "limit": limit}
    if stage_id is not None:
        conditions.append(
            "l.mp_match_id IN (SELECT h.mp_match_id FROM lobby_pool_hits h "
            "JOIN pool_maps p ON p.id = h.pool_map_id WHERE p.stage_id = :stage)"
        )
        params["stage"] = stage_id
    if osu_id is not None:
        conditions.append(
            "l.mp_match_id IN (SELECT mp_match_id FROM lobby_players "
            "WHERE tournament_id = :tid AND osu_id = :player)"
        )
        params["player"] = osu_id
    rows = conn.execute(
        "SELECT l.mp_match_id, l.name, l.started_at, l.ended_at, l.first_seen_at, "  # noqa: S608
        "l.last_scanned_at, l.is_complete, l.discovered_by FROM lobbies l "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY COALESCE(l.started_at, l.first_seen_at) DESC, l.mp_match_id DESC LIMIT :limit",
        params,
    ).fetchall()
    return [_lobby(row) for row in rows]


def hits_for(
    conn: sqlite3.Connection, mp_match_ids: Collection[int], tournament_id: int
) -> list[HitRow]:
    """The pool maps of this tournament that were played in the given lobbies."""
    if not mp_match_ids:
        return []
    marks = ", ".join("?" for _ in mp_match_ids)
    rows = conn.execute(
        "SELECT DISTINCT h.mp_match_id, p.id AS pool_map_id, p.slot, p.stage_id, "  # noqa: S608
        "p.sort_order FROM lobby_pool_hits h JOIN pool_maps p ON p.id = h.pool_map_id "
        "JOIN stages s ON s.id = p.stage_id "
        f"WHERE s.tournament_id = ? AND h.mp_match_id IN ({marks}) "
        "ORDER BY h.mp_match_id, s.sort_order, p.sort_order, p.id",
        [tournament_id, *mp_match_ids],
    ).fetchall()
    return [HitRow(**row) for row in rows]


_PLAYER_COLUMNS = (
    "lp.mp_match_id, lp.osu_id, lp.team_id, t.name AS team_name, "
    "COALESCE(tp.username, u.username) AS username"
)
_PLAYER_JOINS = (
    "FROM lobby_players lp LEFT JOIN teams t ON t.id = lp.team_id "
    "LEFT JOIN team_players tp ON tp.team_id = lp.team_id AND tp.osu_id = lp.osu_id "
    "LEFT JOIN users u ON u.osu_id = lp.osu_id "
)


def players_for(
    conn: sqlite3.Connection, mp_match_ids: Collection[int], tournament_id: int
) -> list[PlayerRow]:
    """The registered players found in the given lobbies, with their team and best known name."""
    if not mp_match_ids:
        return []
    marks = ", ".join("?" for _ in mp_match_ids)
    rows = conn.execute(
        f"SELECT {_PLAYER_COLUMNS} {_PLAYER_JOINS}"  # noqa: S608
        f"WHERE lp.tournament_id = ? AND lp.mp_match_id IN ({marks}) "
        "ORDER BY lp.mp_match_id, t.name COLLATE NOCASE, username COLLATE NOCASE, lp.osu_id",
        [tournament_id, *mp_match_ids],
    ).fetchall()
    return [PlayerRow(**row) for row in rows]


def players_of_tournament(conn: sqlite3.Connection, tournament_id: int) -> list[PlayerRow]:
    """Every registered player found in any lobby of the tournament, one row each."""
    rows = conn.execute(
        f"SELECT {_PLAYER_COLUMNS} {_PLAYER_JOINS}"  # noqa: S608
        "WHERE lp.tournament_id = ? GROUP BY lp.osu_id "
        "ORDER BY username COLLATE NOCASE, lp.osu_id",
        (tournament_id,),
    ).fetchall()
    return [PlayerRow(**row) for row in rows]


def _lobby(row: sqlite3.Row) -> Lobby:
    values = dict(row)
    values["is_complete"] = bool(values["is_complete"])
    return Lobby(**values)
