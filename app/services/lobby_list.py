"""The lobbies page of a tournament: stored lobbies with the pool maps played and the registered
players found, grouped by team."""

import sqlite3
from dataclasses import dataclass

from app.domain.models import Tournament
from app.repos import lobbies as lobbies_repo
from app.repos import members as members_repo
from app.repos.lobbies import Lobby, PlayerRow

LOBBY_LIMIT = 100
NO_TEAM = "No team"


@dataclass(frozen=True)
class PlayerEntry:
    osu_id: int
    name: str
    ours: bool  # on our roster or our team


@dataclass(frozen=True)
class TeamGroup:
    name: str
    players: list[PlayerEntry]


@dataclass(frozen=True)
class LobbyRow:
    lobby: Lobby
    link: str
    maps: list[str]  # slots of the pool maps that were played
    teams: list[TeamGroup]


def match_link(mp_match_id: int) -> str:
    """The osu! address of a lobby, rebuilt from its id."""
    return f"https://osu.ppy.sh/community/matches/{mp_match_id}"


def list_lobbies(
    conn: sqlite3.Connection,
    tournament: Tournament,
    stage_id: int | None,
    osu_id: int | None,
) -> list[LobbyRow]:
    """Up to `LOBBY_LIMIT` lobbies, newest first, narrowed by stage and/or player."""
    lobbies = lobbies_repo.list_for_tournament(conn, tournament.id, stage_id, osu_id, LOBBY_LIMIT)
    ids = [lobby.mp_match_id for lobby in lobbies]
    maps: dict[int, list[str]] = {}
    for hit in lobbies_repo.hits_for(conn, ids, tournament.id):
        maps.setdefault(hit.mp_match_id, []).append(hit.slot)
    roster = {
        member.osu_id
        for member in members_repo.list_for_tournament(conn, tournament.id)
        if member.role != "guest"
    }
    players: dict[int, list[PlayerRow]] = {}
    for row in lobbies_repo.players_for(conn, ids, tournament.id):
        players.setdefault(row.mp_match_id, []).append(row)
    return [
        LobbyRow(
            lobby,
            match_link(lobby.mp_match_id),
            maps.get(lobby.mp_match_id, []),
            _group(players.get(lobby.mp_match_id, []), roster, tournament.our_team_id),
        )
        for lobby in lobbies
    ]


def player_options(conn: sqlite3.Connection, tournament: Tournament) -> list[tuple[str, str]]:
    """(value, label) pairs for the player filter: everyone found in any lobby so far."""
    return [
        (str(row.osu_id), row.username or f"osu! user {row.osu_id}")
        for row in lobbies_repo.players_of_tournament(conn, tournament.id)
    ]


def _group(rows: list[PlayerRow], roster: set[int], our_team_id: int | None) -> list[TeamGroup]:
    """Players by team; the team that has our players comes first."""
    groups: dict[str, list[PlayerEntry]] = {}
    for row in rows:
        ours = row.osu_id in roster or (our_team_id is not None and row.team_id == our_team_id)
        entry = PlayerEntry(row.osu_id, row.username or f"osu! user {row.osu_id}", ours)
        groups.setdefault(row.team_name or NO_TEAM, []).append(entry)
    ordered = sorted(groups.items(), key=lambda item: not any(p.ours for p in item[1]))
    return [TeamGroup(name, entries) for name, entries in ordered]
