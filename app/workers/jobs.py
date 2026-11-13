"""The background jobs of this version. Each opens its own database connection per run."""

import asyncio
from contextlib import closing
from datetime import timedelta

from app.clients.osu import OsuClient
from app.clients.wybin import WybinClient
from app.clock import Clock
from app.config import Settings
from app.db.connection import open_db
from app.services.score_capture import DRAIN_BATCH, drain, enqueue_active
from app.services.wybin_sync import sync_active, track_signups
from app.workers.scheduler import Job

TRACK_SIGNUPS = "wybin_track_signups"
SYNC_ACTIVE = "wybin_sync_active"
CAPTURE_ENQUEUE = "capture_enqueue_active"
CAPTURE_DRAIN = "capture_drain"
TRACK_SIGNUPS_INTERVAL = timedelta(hours=6)
CAPTURE_ENQUEUE_INTERVAL = timedelta(hours=1)
CAPTURE_DRAIN_INTERVAL = timedelta(minutes=1)
JOB_LABELS = {
    TRACK_SIGNUPS: "Find tournaments admins signed up for on wybin",
    SYNC_ACTIVE: "Update active wybin tournaments",
    CAPTURE_ENQUEUE: "Queue osu! score pulls for active tournaments",
    CAPTURE_DRAIN: "Pull queued scores from osu!",
}


def build_jobs(settings: Settings, clock: Clock, wybin: WybinClient, osu: OsuClient) -> list[Job]:
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

    async def enqueue() -> None:
        with closing(open_db(settings.db_path)) as conn:
            enqueue_active(conn, clock, settings.capture_refresh_hours)

    async def pull() -> None:
        async with drain_lock:
            with closing(open_db(settings.db_path)) as conn:
                await drain(conn, osu, clock, DRAIN_BATCH)

    return [
        Job(TRACK_SIGNUPS, TRACK_SIGNUPS_INTERVAL, track),
        Job(SYNC_ACTIVE, timedelta(minutes=settings.wybin_poll_minutes), sync),
        Job(CAPTURE_ENQUEUE, CAPTURE_ENQUEUE_INTERVAL, enqueue),
        Job(CAPTURE_DRAIN, CAPTURE_DRAIN_INTERVAL, pull),
    ]
