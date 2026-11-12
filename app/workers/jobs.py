"""The background jobs of this version. Each opens its own database connection per run."""

from contextlib import closing
from datetime import timedelta

from app.clients.wybin import WybinClient
from app.clock import Clock
from app.config import Settings
from app.db.connection import open_db
from app.services.wybin_sync import sync_active, track_signups
from app.workers.scheduler import Job

TRACK_SIGNUPS = "wybin_track_signups"
SYNC_ACTIVE = "wybin_sync_active"
TRACK_SIGNUPS_INTERVAL = timedelta(hours=6)
JOB_LABELS = {
    TRACK_SIGNUPS: "Find tournaments admins signed up for on wybin",
    SYNC_ACTIVE: "Update active wybin tournaments",
}


def build_jobs(settings: Settings, clock: Clock, wybin: WybinClient) -> list[Job]:
    async def track() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await track_signups(conn, wybin, clock)

    async def sync() -> None:
        with closing(open_db(settings.db_path)) as conn:
            await sync_active(conn, wybin, clock)

    return [
        Job(TRACK_SIGNUPS, TRACK_SIGNUPS_INTERVAL, track),
        Job(SYNC_ACTIVE, timedelta(minutes=settings.wybin_poll_minutes), sync),
    ]
