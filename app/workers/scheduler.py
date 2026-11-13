"""Runs registered jobs on fixed intervals in the app's own event loop.

Each job has its own task, so a slow or failing job never delays or stops the others. The outcome
of every run is stored in `job_state` (last run, last success, a generic error text); details go
to the server log only. Shutdown cancels the tasks and waits for them.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from app.clients.osu import OsuError
from app.clients.wybin_models import WybinError
from app.clock import Clock, to_utc_iso
from app.db.connection import open_db
from app.repos import jobs as jobs_repo
from app.services.wybin_sync import WybinSyncError

logger = logging.getLogger(__name__)

# Shown on the admin page. Never a request body, URL or exception text.
WYBIN_ERROR_TEXT = "wybin could not be read. Nothing was changed for what failed."
OSU_ERROR_TEXT = "osu! could not be reached. The work is picked up again on the next run."
GENERIC_ERROR_TEXT = "The job failed. See the server log for details."

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    name: str
    interval: timedelta
    run: Callable[[], Awaitable[None]]


class Scheduler:
    def __init__(
        self, db_path: Path, clock: Clock, jobs: list[Job], sleep: Sleep = asyncio.sleep
    ) -> None:
        self._db_path = db_path
        self._clock = clock
        self._jobs = jobs
        self._sleep = sleep
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        """Start one task per job; the first run happens right away."""
        self._tasks = [
            asyncio.create_task(self._loop(job), name=f"job-{job.name}") for job in self._jobs
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def job(self, name: str) -> Job:
        return next(job for job in self._jobs if job.name == name)

    async def run_once(self, job: Job) -> bool:
        """Run a job now and record the outcome. Returns whether it succeeded."""
        with closing(open_db(self._db_path)) as conn:
            jobs_repo.record_start(conn, job.name, to_utc_iso(self._clock.now()))
        try:
            await job.run()
        except asyncio.CancelledError:
            raise
        except WybinSyncError as error:
            # The message only counts tournaments and names no URL, so it is safe to show.
            logger.warning("Job %s: %s", job.name, error)
            self._record_failure(job, str(error))
            return False
        except WybinError as error:
            logger.warning("Job %s: wybin problem: %s", job.name, type(error).__name__)
            self._record_failure(job, WYBIN_ERROR_TEXT)
            return False
        except OsuError as error:
            logger.warning("Job %s: osu! problem: %s", job.name, type(error).__name__)
            self._record_failure(job, OSU_ERROR_TEXT)
            return False
        except Exception:
            logger.exception("Job %s failed", job.name)
            self._record_failure(job, GENERIC_ERROR_TEXT)
            return False
        with closing(open_db(self._db_path)) as conn:
            jobs_repo.record_success(conn, job.name, to_utc_iso(self._clock.now()))
        return True

    def _record_failure(self, job: Job, message: str) -> None:
        with closing(open_db(self._db_path)) as conn:
            jobs_repo.record_failure(conn, job.name, message)

    async def _loop(self, job: Job) -> None:
        while True:
            await self.run_once(job)
            await self._sleep(job.interval.total_seconds())
