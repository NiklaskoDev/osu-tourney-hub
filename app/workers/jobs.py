"""The background jobs of this version. Each opens its own database connection per run."""

import asyncio
from contextlib import closing
from datetime import timedelta

from app.clients.osu import OsuClient
from app.clients.wybin import WybinClient
from app.clock import Clock
from app.config import Settings
from app.db.connection import open_db
from app.services.history import import_pending
from app.services.live import LiveFollower
from app.services.lobbies import import_wybin_links
from app.services.score_capture import DRAIN_BATCH, drain, enqueue_active
from app.services.skimmer import Skimmer
from app.services.wybin_sync import sync_active, track_signups
from app.workers.scheduler import Job

TRACK_SIGNUPS = "wybin_track_signups"
SYNC_ACTIVE = "wybin_sync_active"
CAPTURE_ENQUEUE = "capture_enqueue_active"
CAPTURE_DRAIN = "capture_drain"
LOBBY_LINKS = "lobby_wybin_links"
MATCH_HISTORY = "match_history"
LIVE_FOLLOW = "live_follow"
SKIMMER_HEAD = "skimmer_head"
SKIMMER_WORKERS = "skimmer_workers"
SKIMMER_OPEN_CHECK = "skimmer_open_check"
SKIMMER_STATS_PRUNE = "skimmer_stats_prune"
TRACK_SIGNUPS_INTERVAL = timedelta(hours=6)
CAPTURE_ENQUEUE_INTERVAL = timedelta(hours=1)
CAPTURE_DRAIN_INTERVAL = timedelta(minutes=1)
LIVE_FOLLOW_INTERVAL = timedelta(seconds=60)
SKIMMER_HEAD_INTERVAL = timedelta(seconds=60)
# The workers run until stopped; this only restarts them after a crash.
SKIMMER_WORKERS_RESTART = timedelta(minutes=1)
SKIMMER_OPEN_CHECK_INTERVAL = timedelta(minutes=5)
SKIMMER_STATS_PRUNE_INTERVAL = timedelta(hours=1)
JOB_LABELS = {
    TRACK_SIGNUPS: "Find tournaments admins signed up for on wybin",
    SYNC_ACTIVE: "Update active wybin tournaments",
    CAPTURE_ENQUEUE: "Queue osu! score pulls for active tournaments",
    CAPTURE_DRAIN: "Pull queued scores from osu!",
    LOBBY_LINKS: "Read multiplayer lobbies linked on wybin",
    MATCH_HISTORY: "Read the lobbies of finished matches for the match history",
    LIVE_FOLLOW: "Start live updates for matches about to begin",
    SKIMMER_HEAD: "Scanner: find the newest multiplayer lobby id",
    SKIMMER_WORKERS: "Scanner: read new multiplayer lobbies (runs continuously)",
    SKIMMER_OPEN_CHECK: "Scanner: read lobbies that were open when seen",
    SKIMMER_STATS_PRUNE: "Scanner: delete counters older than a day",
}
SKIMMER_JOBS = frozenset({SKIMMER_HEAD, SKIMMER_WORKERS, SKIMMER_OPEN_CHECK, SKIMMER_STATS_PRUNE})
CONTINUOUS_JOBS = frozenset({SKIMMER_WORKERS})  # they never finish, so "last success" stays empty


def build_jobs(
    settings: Settings,
    clock: Clock,
    wybin: WybinClient,
    osu: OsuClient,
    skimmer: Skimmer,
    live: LiveFollower,
) -> list[Job]:
    # The drain runs from its timer and when a person presses "Refresh"; one at a time, so two runs
    # never pull the same queue entry twice.
    drain_lock = asyncio.Lock()

    async def track() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await track_signups(conn, wybin, clock)

    async def sync() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await sync_active(conn, wybin, clock)
            # Maps the sync added are pulled soon; pairs pulled recently are skipped. After a
            # failed sync the hourly enqueue job queues whatever the sync managed to add, so the
            # sync's own error is the one that gets recorded.
            enqueue_active(conn, clock, settings.capture_refresh_hours)
            skimmer.pool.invalidate()  # the sync may have changed which maps are current

    async def enqueue() -> None:
        with closing(open_db(settings.db_path)) as conn:
            enqueue_active(conn, clock, settings.capture_refresh_hours)

    async def pull() -> None:
        async with drain_lock:
            with closing(open_db(settings.db_path)) as conn:
                await drain(conn, osu, clock, DRAIN_BATCH)

    async def read_wybin_links() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await import_wybin_links(conn, osu, clock.now())

    async def read_history() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await import_pending(conn, osu, clock.now())

    async def start_live() -> None:
        await live.start_due()

    jobs = [
        Job(TRACK_SIGNUPS, TRACK_SIGNUPS_INTERVAL, track),
        Job(SYNC_ACTIVE, timedelta(minutes=settings.wybin_poll_minutes), sync),
        Job(CAPTURE_ENQUEUE, CAPTURE_ENQUEUE_INTERVAL, enqueue),
        Job(CAPTURE_DRAIN, CAPTURE_DRAIN_INTERVAL, pull),
        Job(LOBBY_LINKS, timedelta(minutes=settings.wybin_poll_minutes), read_wybin_links),
        Job(MATCH_HISTORY, timedelta(minutes=settings.wybin_poll_minutes), read_history),
        Job(LIVE_FOLLOW, LIVE_FOLLOW_INTERVAL, start_live),
    ]
    if settings.scanner_enabled:
        jobs += _skimmer_jobs(settings, skimmer)
    return jobs


def _skimmer_jobs(settings: Settings, skimmer: Skimmer) -> list[Job]:
    async def head() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await skimmer.check_head(conn)

    async def check_open() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await skimmer.check_open(conn)

    async def prune() -> None:
        with closing(open_db(settings.db_path)) as conn:
            skimmer.prune_stats(conn)

    return [
        Job(SKIMMER_HEAD, SKIMMER_HEAD_INTERVAL, head),
        Job(SKIMMER_WORKERS, SKIMMER_WORKERS_RESTART, skimmer.run_workers),
        Job(SKIMMER_OPEN_CHECK, SKIMMER_OPEN_CHECK_INTERVAL, check_open),
        Job(SKIMMER_STATS_PRUNE, SKIMMER_STATS_PRUNE_INTERVAL, prune),
    ]
