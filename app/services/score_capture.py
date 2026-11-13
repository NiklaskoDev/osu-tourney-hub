"""Automatic score capture: which player-and-map pulls to queue, and running the queue.

A pull asks osu! for every score one player has on one beatmap (ranked and loved maps only; osu!
keeps nothing for others). Pulls wait in `capture_queue`; `drain` works through them at the
`CAPTURE` rate-limit priority, so live and interactive calls always go first. Captured scores are
stored once per osu! score id (D3: lazer standardised score, which also counts on ScoreV2
tournaments, marked "lazer", per plan 002; D6: any date, marked old later).
"""

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clients.osu import OsuClient, OsuError
from app.clients.ratelimit import Priority
from app.clock import Clock, to_utc_iso
from app.db.connection import transaction
from app.domain.models import Stage, Tournament
from app.repos import capture as capture_repo
from app.repos import members as members_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo

logger = logging.getLogger(__name__)

# Lower runs first. A person asked for it, or the app decided to.
PRIORITY_REQUESTED = 0
PRIORITY_AUTOMATIC = 1
MAX_ATTEMPTS = 3
STAGES_TO_CAPTURE = 2  # the stage being played and the one after it
DRAIN_BATCH = 30


@dataclass(frozen=True)
class DrainResult:
    pulled: int = 0
    new_scores: int = 0


def capture_stages(stages: Iterable[Stage], now: datetime) -> list[Stage]:
    """The stage being played and the next one: the first stages (in order) that have not ended.
    A stage without an end date has not ended."""
    stamp = to_utc_iso(now)
    open_stages = [stage for stage in stages if stage.end_at is None or stage.end_at > stamp]
    return open_stages[:STAGES_TO_CAPTURE]


def capture_beatmap_ids(
    conn: sqlite3.Connection, tournament: Tournament, now: datetime
) -> list[int]:
    """Beatmaps of the pools to capture, each once (maps without a beatmap are skipped)."""
    ids: dict[int, None] = {}
    for stage in capture_stages(stages_repo.list_for_tournament(conn, tournament.id), now):
        for pool_map in stages_repo.list_maps(conn, stage.id):
            if pool_map.beatmap_id is not None:
                ids[pool_map.beatmap_id] = None
    return list(ids)


def enqueue_players(
    conn: sqlite3.Connection,
    tournament: Tournament,
    osu_ids: Iterable[int],
    priority: int,
    now: datetime,
    skip_within: timedelta | None,
) -> int:
    """Queue every given player on every map to capture. With `skip_within`, pairs pulled that
    recently are left out (automatic runs); without it everything is queued (a person asked).
    Returns how many pulls were queued."""
    beatmaps = capture_beatmap_ids(conn, tournament, now)
    pairs = {(osu_id, beatmap_id) for osu_id in osu_ids for beatmap_id in beatmaps}
    if skip_within is not None:
        cutoff = to_utc_iso(now - skip_within)
        pairs -= capture_repo.pulled_since(conn, pairs, tournament.gamemode, cutoff)
    with transaction(conn):
        capture_repo.enqueue(conn, pairs, tournament.gamemode, priority, to_utc_iso(now))
    return len(pairs)


def enqueue_for_tournament(
    conn: sqlite3.Connection,
    tournament: Tournament,
    priority: int,
    now: datetime,
    skip_within: timedelta | None,
) -> int:
    """Queue the roster (everyone but guests) of a tournament."""
    members = members_repo.list_for_tournament(conn, tournament.id)
    osu_ids = [member.osu_id for member in members if member.role != "guest"]
    return enqueue_players(conn, tournament, osu_ids, priority, now, skip_within)


def enqueue_for_team(
    conn: sqlite3.Connection, tournament: Tournament, team_id: int, priority: int, now: datetime
) -> int:
    """Queue every player of one team (an opponent, usually), whether or not pulled recently."""
    osu_ids = [player.osu_id for player in teams_repo.list_players(conn, team_id)]
    return enqueue_players(conn, tournament, osu_ids, priority, now, None)


def enqueue_active(conn: sqlite3.Connection, clock: Clock, refresh_hours: int) -> int:
    """Queue the rosters of all active tournaments, skipping pulls newer than `refresh_hours`.
    Running it again is cheap: only new players, new maps and stale pulls are queued."""
    now = clock.now()
    queued = 0
    for tournament in tournaments_repo.list_active(conn):
        queued += enqueue_for_tournament(
            conn, tournament, PRIORITY_AUTOMATIC, now, timedelta(hours=refresh_hours)
        )
    return queued


async def drain(
    conn: sqlite3.Connection, osu: OsuClient, clock: Clock, limit: int = DRAIN_BATCH
) -> DrainResult:
    """Run up to `limit` queued pulls. When osu! cannot be reached the failed pull is counted
    (and dropped after `MAX_ATTEMPTS`), the rest stay queued, and the error is raised so the job
    shows it; what was already captured is kept."""
    pulled = new_scores = 0
    for item in capture_repo.next_batch(conn, limit):
        try:
            scores = await osu.get_user_beatmap_scores(
                item.beatmap_id, item.osu_id, item.gamemode, Priority.CAPTURE
            )
        except OsuError:
            dropped = capture_repo.record_failure(conn, item, MAX_ATTEMPTS)
            logger.warning(
                "osu! score pull for player %s on beatmap %s failed (dropped: %s)",
                item.osu_id,
                item.beatmap_id,
                dropped,
            )
            raise
        now = to_utc_iso(clock.now())
        with transaction(conn):
            new_scores += sum(
                scores_repo.insert_api(conn, score, item.gamemode, now) for score in scores
            )
            capture_repo.mark_pulled(conn, item, now)
        pulled += 1
    return DrainResult(pulled, new_scores)
