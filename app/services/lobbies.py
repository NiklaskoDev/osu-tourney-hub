"""Storing a multiplayer lobby: which of its games were played on pool maps, which registered
players took part, and their scores. Shared by the scanner, wybin lobby links and links a member
pastes, so one lobby read by two of them is stored once (every insert is idempotent).

New file next to `skimmer.py`: the scanner decides which lobbies to read; this decides what a read
lobby leaves behind.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clients.osu import OsuClient, OsuNotFound
from app.clients.ratelimit import Priority
from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import FetchedMatch, MpGame
from app.repos import app_settings as app_settings_repo
from app.repos import lobbies as lobbies_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.repos.stages import ScanMap
from app.services import mp_parse

logger = logging.getLogger(__name__)

POOL_INDEX_TTL = timedelta(minutes=5)
WYBIN_LINKS_PER_RUN = 20
MISSING_PREFIX = "lobby_missing:"  # app_settings counters of wybin links osu! does not have
MAX_MISSING_READS = 3


@dataclass(frozen=True)
class PoolIndex:
    """The pool maps worth finding, by beatmap id. One beatmap can sit in several tournaments."""

    by_beatmap: dict[int, list[ScanMap]]

    def hits(self, game: MpGame) -> list[ScanMap]:
        """The pool maps this game was played on: same beatmap, same game mode."""
        return [m for m in self.by_beatmap.get(game.beatmap_id, []) if m.gamemode == game.gamemode]


def build_pool_index(
    conn: sqlite3.Connection,
    now: datetime,
    tournament_id: int | None = None,
    include_ended: bool = False,
) -> PoolIndex:
    """The maps of every released stage of every active tournament whose end date is empty or not
    past: the round being played and the next one once its pool is out. With `tournament_id` only
    that tournament, whatever its status; with `include_ended` also its finished stages."""
    by_beatmap: dict[int, list[ScanMap]] = {}
    for scan_map in stages_repo.list_scan_maps(conn, to_utc_iso(now), tournament_id, include_ended):
        by_beatmap.setdefault(scan_map.beatmap_id, []).append(scan_map)
    return PoolIndex(by_beatmap)


class PoolIndexCache:
    """The scanner's pool index, rebuilt every `POOL_INDEX_TTL` and whenever a pool sync says the
    pools changed."""

    def __init__(self) -> None:
        self._index: PoolIndex | None = None
        self._built_at: datetime | None = None

    def invalidate(self) -> None:
        self._index = None

    def get(self, conn: sqlite3.Connection, now: datetime) -> PoolIndex:
        expired = self._built_at is None or now - self._built_at >= POOL_INDEX_TTL
        if self._index is None or expired:
            self._index = build_pool_index(conn, now)
            self._built_at = now
        return self._index


@dataclass(frozen=True)
class ProcessResult:
    hit_games: int = 0  # games played on a pool map
    stored: bool = False  # whether the lobby is in the lobbies table now
    new_scores: int = 0


def process_match(
    conn: sqlite3.Connection,
    index: PoolIndex,
    fetched: FetchedMatch,
    discovered_by: str,
    now: datetime,
    final: bool = False,
    keep_for_tournament: int | None = None,
) -> ProcessResult:
    """Store what a lobby read leaves behind.

    Every finished game played on a pool map (in the index) is recorded as a hit, and so are the
    scores of the players registered in that tournament, whichever team they play for. A lobby
    without a hit is stored only when `keep_for_tournament` is given (a link from wybin or a
    member); then the tournament's registered players of the lobby are recorded too. The lobby is
    complete when osu! says it has ended, or when `final` says this was the last read.
    """
    games = mp_parse.parse_games(fetched.events)
    hits = [(game, index.hits(game)) for game in games]
    hits = [(game, maps) for game, maps in hits if maps]
    complete = final or fetched.match.end_time is not None
    if not hits and keep_for_tournament is None:
        # Nothing new on pool maps, but a lobby stored earlier may just have closed.
        refreshed = lobbies_repo.refresh(
            conn,
            fetched.match.id,
            fetched.match.name,
            fetched.match.end_time,
            to_utc_iso(now),
            complete,
        )
        return ProcessResult(stored=refreshed)
    tournament_ids = {scan_map.tournament_id for _, maps in hits for scan_map in maps}
    if keep_for_tournament is not None:
        tournament_ids.add(keep_for_tournament)
    registered = lobbies_repo.registered_players(conn, tournament_ids)
    stamp = to_utc_iso(now)
    mp_id = fetched.match.id
    new_scores = 0
    with transaction(conn):
        lobbies_repo.upsert(
            conn,
            mp_id,
            fetched.match.name,
            fetched.match.start_time,
            fetched.match.end_time,
            stamp,
            complete=complete,
            discovered_by=discovered_by,
        )
        for game, maps in hits:
            counted: set[int] = set()
            for scan_map in maps:
                lobbies_repo.insert_hit(
                    conn, mp_id, game.game_id, scan_map.pool_map_id, game.played_at, game.mods
                )
                players = registered[scan_map.tournament_id]
                for score in game.scores:
                    if score.osu_id in players:
                        counted.add(score.osu_id)
                        lobbies_repo.insert_player(
                            conn, mp_id, scan_map.tournament_id, score.osu_id, players[score.osu_id]
                        )
            for score in game.scores:
                if score.osu_id in counted:
                    new_scores += scores_repo.insert_mp(
                        conn,
                        mp_id,
                        game.game_id,
                        game.beatmap_id,
                        game.gamemode,
                        game.scoring,
                        game.played_at,
                        score,
                        stamp,
                    )
        if keep_for_tournament is not None:
            players = registered[keep_for_tournament]
            for game in games:
                for score in game.scores:
                    if score.osu_id in players:
                        lobbies_repo.insert_player(
                            conn, mp_id, keep_for_tournament, score.osu_id, players[score.osu_id]
                        )
    if fetched.truncated:
        logger.warning("Lobby %s has more events than were read; the oldest are missing", mp_id)
    return ProcessResult(len(hits), True, new_scores)


async def import_lobby(
    conn: sqlite3.Connection,
    osu: OsuClient,
    mp_match_id: int,
    tournament_id: int,
    discovered_by: str,
    now: datetime,
) -> ProcessResult:
    """Read one lobby named by a person or by wybin and store it for that tournament, hits or not.
    Hits are looked up in all released stages of the tournament, ended ones included. Raises
    `OsuNotFound` for a deleted or private lobby and `OsuUnavailable` when osu! fails."""
    fetched = await osu.fetch_match(mp_match_id, priority=Priority.INTERACTIVE)
    index = build_pool_index(conn, now, tournament_id, include_ended=True)
    return process_match(
        conn, index, fetched, discovered_by, now, keep_for_tournament=tournament_id
    )


async def import_wybin_links(
    conn: sqlite3.Connection, osu: OsuClient, now: datetime, limit: int = WYBIN_LINKS_PER_RUN
) -> int:
    """Read the lobbies wybin links for matches of active tournaments, a few per run. A link osu!
    does not know is skipped, and given up on after `MAX_MISSING_READS` tries. When osu! fails the
    error is raised and the rest waits for the next run. Returns how many lobbies were read."""
    given_up = [
        int(key.removeprefix(MISSING_PREFIX))
        for key, value in app_settings_repo.get_prefixed(conn, MISSING_PREFIX).items()
        if value.isdecimal() and int(value) >= MAX_MISSING_READS
    ]
    read = 0
    for link in lobbies_repo.wybin_links_to_fetch(conn, limit, given_up):
        try:
            await import_lobby(conn, osu, link.mp_match_id, link.tournament_id, "wybin", now)
        except OsuNotFound:
            key = f"{MISSING_PREFIX}{link.mp_match_id}"
            misses = int(app_settings_repo.get(conn, key) or 0) + 1
            app_settings_repo.put(conn, key, str(misses))
            logger.info("wybin links lobby %s, which osu! does not have", link.mp_match_id)
            continue
        read += 1
    return read
