"""The lobby scanner (decision D5): reads every new osu! multiplayer match id, whatever the lobby is
called, and keeps the lobbies in which a map of a current pool was played.

How it fits together
- `check_head` (every minute) asks osu! for the newest lobby ids. The first time it only notes the
  newest id: history is never backfilled. Afterwards the ids between the saved cursor and the head
  are handed to the workers by an `IdDispenser`.
- `run_workers` runs `SCANNER_WORKERS` tasks that each take an id, read the lobby and store what it
  leaves behind (`services.lobbies.process_match`). The cursor is the highest id below which every
  id is done; it is saved every few seconds and on shutdown.
- A lobby that is still open is not stored half-read. It waits in `scan_open` and `check_open`
  (every 5 minutes) reads it once it closed, or in steps when it stays open for hours.
- When the scanner falls too far behind it skips ahead and records the skipped range in
  `scan_gaps`; an admin can ask for a gap to be backfilled with budget that is left over.
Every request is counted against `SCANNER_BUDGET_PER_MIN` by the osu! client.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clients.osu import (
    MATCH_MAX_PAGES,
    OsuClient,
    OsuError,
    OsuNotFound,
    tally_scan_requests,
)
from app.clients.ratelimit import Priority
from app.clock import Clock, from_utc_iso, to_utc_iso
from app.config import Settings
from app.db.connection import open_db
from app.domain.models import FetchedMatch
from app.repos import app_settings as app_settings_repo
from app.repos import jobs as jobs_repo
from app.repos import lobbies as lobbies_repo
from app.repos import scan as scan_repo
from app.repos.scan import Gap, OpenRow, StatRow
from app.services import mp_parse
from app.services.lobbies import PoolIndexCache, process_match

logger = logging.getLogger(__name__)

PAUSED_KEY = "scanner_paused"
LAST_ERROR_KEY = "scanner_last_error"
CURSOR_JOB = "skimmer"  # job_state row holding the cursor (not a scheduled job)
HEAD_JOB = "skimmer_head"
HEAD_MAX_PAGES = 3  # 150 ids per run are checked for lobbies that are still open
ACTIVE_MAX_PAGES = 20
MAX_ATTEMPTS = 3
CURSOR_FLUSH_SECONDS = 10.0
IDLE_SECONDS = 1.0
LONG_LOBBY = timedelta(hours=2)
LONG_LOBBY_STEP = timedelta(minutes=30)
RETRY_DELAY = timedelta(seconds=30)  # times the number of failures so far
BACKFILL_MAX_LIVE_BACKLOG = 100
STATS_KEEP = timedelta(hours=24)
CAUGHT_UP_BACKLOG = 20  # ids behind the head that still count as keeping up
SHORT_WINDOW_MINUTES = 15
LONG_WINDOW_MINUTES = 60
MINUTE_FORMAT = "%Y-%m-%dT%H:%M"

Sleep = Callable[[float], Awaitable[None]]


class IdDispenser:
    """Hands out match ids in ascending order and tracks which are done.

    `cursor` is the highest id below which (and at which) every id is done, so a crash only ever
    repeats a few ids. Ids may finish in any order; the cursor only moves over a contiguous run.
    """

    def __init__(self, cursor: int, head: int) -> None:
        self._cursor = cursor
        self._next = cursor + 1
        self._head = max(head, cursor)
        self._done: set[int] = set()
        self._retry: list[tuple[datetime, int]] = []  # (not before, id), oldest first

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def head(self) -> int:
        return self._head

    @property
    def backlog(self) -> int:
        return self._head - self._cursor

    def raise_head(self, head: int) -> None:
        self._head = max(self._head, head)

    def take(self, now: datetime) -> int | None:
        """The next id to read: one whose retry is due, else the next never handed out."""
        for index, (not_before, match_id) in enumerate(self._retry):
            if not_before <= now:
                del self._retry[index]
                return match_id
        if self._next <= self._head:
            taken = self._next
            self._next += 1
            return taken
        return None

    def done(self, match_id: int) -> None:
        if match_id > self._cursor:
            self._done.add(match_id)
            self._advance()

    def retry(self, match_id: int, not_before: datetime) -> None:
        self._retry.append((not_before, match_id))

    def skip_to(self, cursor: int) -> None:
        """Give up on every id up to `cursor` (skip-ahead)."""
        self._cursor = max(self._cursor, cursor)
        self._next = max(self._next, self._cursor + 1)
        self._retry = [item for item in self._retry if item[1] > self._cursor]
        self._advance()

    def _advance(self) -> None:
        self._done = {item for item in self._done if item > self._cursor}
        while self._cursor + 1 in self._done:
            self._cursor += 1
            self._done.discard(self._cursor)


class Skimmer:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        osu: OsuClient,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._osu = osu
        self._sleep = sleep
        self.pool = PoolIndexCache()
        self._dispenser: IdDispenser | None = None
        self._known_open: set[int] = set()
        self._attempts: dict[int, int] = {}
        self._reported_requests = 0
        self._saved_cursor: int | None = None

    # --- switches -------------------------------------------------------------------------------

    @staticmethod
    def is_paused(conn: sqlite3.Connection) -> bool:
        return app_settings_repo.get(conn, PAUSED_KEY) == "1"

    @staticmethod
    def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
        app_settings_repo.put(conn, PAUSED_KEY, "1" if paused else "0")

    # --- head -----------------------------------------------------------------------------------

    async def check_head(self, conn: sqlite3.Connection) -> None:
        """Learn the newest match id, start the scan on the first run, remember lobbies that are
        still open, and skip ahead when the scanner is too far behind."""
        if self.is_paused(conn):
            return
        matches, next_page = await self._osu.list_matches()
        if not matches:
            return
        head_id = max(match.id for match in matches)
        jobs_repo.set_cursor(conn, HEAD_JOB, str(head_id))
        self._count(conn, head_id=head_id)
        cursor = self._stored_cursor(conn)
        if cursor is None:
            cursor = head_id  # the first run never reads history
            jobs_repo.set_cursor(conn, CURSOR_JOB, str(cursor))
            self._saved_cursor = cursor
        collected = list(matches)
        for _ in range(HEAD_MAX_PAGES - 1):
            if not next_page or min(match.id for match in collected) <= cursor:
                break
            page, next_page = await self._osu.list_matches(next_page)
            collected.extend(page)
        self._known_open |= {m.id for m in collected if m.end_time is None and m.id > cursor}
        if self._dispenser is None:
            self._dispenser = IdDispenser(cursor, head_id)
        else:
            self._dispenser.raise_head(head_id)
        self._skip_ahead_if_behind(conn, head_id)
        self._known_open = {i for i in self._known_open if i > self._dispenser.cursor}

    def _skip_ahead_if_behind(self, conn: sqlite3.Connection, head_id: int) -> None:
        dispenser = self._dispenser
        if dispenser is None:
            return
        limit = self._settings.scanner_max_backlog
        if head_id - dispenser.cursor <= limit:
            return
        start, end = dispenser.cursor + 1, head_id - limit
        scan_repo.insert_gap(conn, start, end, "skipped_backlog", to_utc_iso(self._clock.now()))
        dispenser.skip_to(end)
        self.flush_cursor(conn)
        logger.warning(
            "The scanner skipped lobby ids %s to %s to catch up with the head", start, end
        )

    def _stored_cursor(self, conn: sqlite3.Connection) -> int | None:
        state = jobs_repo.get(conn, CURSOR_JOB)
        if state is None or not state.cursor or not state.cursor.isdecimal():
            return None
        return int(state.cursor)

    def flush_cursor(self, conn: sqlite3.Connection) -> None:
        """Save the cursor if it moved."""
        if self._dispenser is None or self._dispenser.cursor == self._saved_cursor:
            return
        jobs_repo.set_cursor(conn, CURSOR_JOB, str(self._dispenser.cursor))
        self._saved_cursor = self._dispenser.cursor

    # --- workers --------------------------------------------------------------------------------

    async def run_workers(self) -> None:
        """Run the scanner workers until cancelled; the cursor is saved on the way out."""
        tasks = [asyncio.create_task(self._worker()) for _ in range(self._settings.scanner_workers)]
        tasks.append(asyncio.create_task(self._flusher()))
        tasks.append(asyncio.create_task(self._backfiller()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with closing(open_db(self._settings.db_path)) as conn:
                self.flush_cursor(conn)

    async def _worker(self) -> None:
        with closing(open_db(self._settings.db_path)) as conn:
            while True:
                if not await self.work_one(conn):
                    await self._sleep(IDLE_SECONDS)

    async def _flusher(self) -> None:
        with closing(open_db(self._settings.db_path)) as conn:
            while True:
                await self._sleep(CURSOR_FLUSH_SECONDS)
                self.flush_cursor(conn)

    async def _backfiller(self) -> None:
        with closing(open_db(self._settings.db_path)) as conn:
            while True:
                if not await self.backfill_one(conn):
                    await self._sleep(IDLE_SECONDS)

    async def work_one(self, conn: sqlite3.Connection) -> bool:
        """Read the next waiting id. False when there is nothing to do (paused, not started, or
        caught up)."""
        dispenser = self._dispenser
        if dispenser is None or self.is_paused(conn):
            return False
        match_id = dispenser.take(self._clock.now())
        if match_id is None:
            return False
        if await self._scan_guarded(conn, match_id):
            dispenser.done(match_id)
        else:
            delay = RETRY_DELAY * self._attempts[match_id]  # a little longer after every failure
            dispenser.retry(match_id, self._clock.now() + delay)
        return True

    async def backfill_one(self, conn: sqlite3.Connection) -> bool:
        """Read the next id of the oldest gap an admin asked to backfill, but only while the live
        scan is nearly caught up. False when there is nothing to do."""
        dispenser = self._dispenser
        if dispenser is None or self.is_paused(conn):
            return False
        if dispenser.backlog >= BACKFILL_MAX_LIVE_BACKLOG:
            return False
        gap = scan_repo.next_backfill(conn)
        if gap is None:
            return False
        match_id = (gap.backfilled_until or 0) + 1  # next_backfill only gives requested gaps
        with tally_scan_requests() as tally:
            done = await self._scan_guarded(conn, match_id, live=False)
        self._reported_requests += tally[0]  # a backfill's requests are not the live scan's
        if done:
            scan_repo.set_backfilled_until(conn, gap.id, match_id)
        return True

    async def _scan_guarded(
        self, conn: sqlite3.Connection, match_id: int, live: bool = True
    ) -> bool:
        """Scan one id. A failure is retried (by the caller) up to `MAX_ATTEMPTS` times, then the
        id is counted as done and the failure is logged. Returns whether the id is done. Only live
        scans (not backfills) go into the counters."""
        try:
            await self.scan_id(conn, match_id, live)
        except Exception as error:  # a broken id must not stop the other workers
            attempts = self._attempts.get(match_id, 0) + 1
            if attempts < MAX_ATTEMPTS:
                self._attempts[match_id] = attempts
                return False
            self._attempts.pop(match_id, None)
            logger.warning(
                "Gave up on lobby %s after %s tries: %s", match_id, attempts, type(error).__name__
            )
            stamp = to_utc_iso(self._clock.now())
            app_settings_repo.put(
                conn,
                LAST_ERROR_KEY,
                f"Lobby {match_id} could not be read after {attempts} tries ({stamp}).",
            )
            if live:
                # A range osu! failed for is kept so an admin can backfill it later.
                scan_repo.record_fetch_error(conn, match_id, stamp)
                self._count(conn, ids_scanned=1, errors=1)
            return True
        self._attempts.pop(match_id, None)
        return True

    async def scan_id(self, conn: sqlite3.Connection, match_id: int, live: bool = True) -> None:
        """Read one match id and store what it leaves behind. Raises `OsuError` when osu! fails.
        With `live=False` (a backfill) the live counters are left alone."""
        now = self._clock.now()
        stamp = to_utc_iso(now)
        if match_id in self._known_open:
            # Seen open in the listing: do not read it half-played, read it once after it closed.
            self._known_open.discard(match_id)
            scan_repo.upsert_open(conn, match_id, stamp, None, 0, stamp)
            if live:
                self._count(conn, ids_scanned=1)
            return
        try:
            fetched = await self._osu.fetch_match(match_id, MATCH_MAX_PAGES, Priority.SCAN)
        except OsuNotFound:
            if live:
                self._count(conn, ids_scanned=1, not_found=1)
            return
        result = process_match(conn, self.pool.get(conn, now), fetched, "scan", now)
        if fetched.match.end_time is None:
            scan_repo.upsert_open(
                conn,
                match_id,
                stamp,
                mp_parse.last_final_event_id(fetched.events),
                len(fetched.events),
                stamp,
            )
        if live:
            self._count(conn, ids_scanned=1, matches_with_hits=int(result.hit_games > 0))

    # --- open lobbies ---------------------------------------------------------------------------

    async def check_open(self, conn: sqlite3.Connection) -> None:
        """Read the lobbies that closed since they were seen open, and the long-running ones in
        steps. A lobby that osu! still lists as active (or that cannot be read right now) stays
        for the next run."""
        if self.is_paused(conn):
            return
        rows = scan_repo.list_open(conn)
        if not rows:
            return
        active = await self._active_ids()
        now = self._clock.now()
        for row in rows:
            if self._is_due(row, active, now):
                await self._check_open_row(conn, row, now)

    async def _active_ids(self) -> set[int]:
        ids: set[int] = set()
        cursor: str | None = None
        for _ in range(ACTIVE_MAX_PAGES):
            page, cursor = await self._osu.list_matches(cursor, active=True)
            ids |= {match.id for match in page}
            if not cursor or not page:
                break
        else:
            logger.warning(
                "More than %s pages of open lobbies; the ones beyond look closed and are read "
                "again every run until they close",
                ACTIVE_MAX_PAGES,
            )
        return ids

    def _is_due(self, row: OpenRow, active: set[int], now: datetime) -> bool:
        age = now - from_utc_iso(row.first_seen_at)
        if age >= timedelta(hours=self._settings.scanner_open_max_hours):
            return True
        listed_closed = row.mp_match_id not in active
        waiting_long = age > LONG_LOBBY
        return (listed_closed or waiting_long) and now >= from_utc_iso(row.next_check_at)

    async def _check_open_row(self, conn: sqlite3.Connection, row: OpenRow, now: datetime) -> None:
        stamp = to_utc_iso(now)
        expired = now - from_utc_iso(row.first_seen_at) >= timedelta(
            hours=self._settings.scanner_open_max_hours
        )
        try:
            fetched = await self._read_after(row)
        except OsuNotFound:
            # Gone for good: what was read stays, but the lobby can not be finished any more.
            lobbies_repo.mark_complete(conn, row.mp_match_id, stamp)
            scan_repo.delete_open(conn, row.mp_match_id)
            return
        except OsuError as error:
            logger.warning(
                "Open lobby %s could not be read: %s", row.mp_match_id, type(error).__name__
            )
            return
        over = fetched.match.end_time is not None or expired
        result = process_match(conn, self.pool.get(conn, now), fetched, "scan", now, final=over)
        if over:
            scan_repo.delete_open(conn, row.mp_match_id)
        else:
            scan_repo.upsert_open(
                conn,
                row.mp_match_id,
                stamp,
                mp_parse.last_final_event_id(fetched.events),
                len(fetched.events),
                to_utc_iso(now + LONG_LOBBY_STEP),
            )
        self._count(conn, matches_with_hits=int(result.hit_games > 0))

    async def _read_after(self, row: OpenRow) -> FetchedMatch:
        if row.last_event_id is None:
            return await self._osu.fetch_match(row.mp_match_id, MATCH_MAX_PAGES, Priority.SCAN)
        return await self._osu.fetch_match_after(
            row.mp_match_id, row.last_event_id, MATCH_MAX_PAGES, Priority.SCAN
        )

    # --- counters -------------------------------------------------------------------------------

    def prune_stats(self, conn: sqlite3.Connection) -> None:
        scan_repo.prune_stats(conn, self._minute(self._clock.now() - STATS_KEEP))

    def _count(
        self,
        conn: sqlite3.Connection,
        ids_scanned: int = 0,
        not_found: int = 0,
        matches_with_hits: int = 0,
        head_id: int | None = None,
        errors: int = 0,
    ) -> None:
        """Add to this minute's counters, with the requests sent since the last call."""
        requests = self._osu.scan_requests - self._reported_requests
        self._reported_requests += requests
        scan_repo.add_stats(
            conn,
            self._minute(self._clock.now()),
            ids_scanned,
            not_found,
            matches_with_hits,
            requests,
            head_id,
            errors,
        )

    @staticmethod
    def _minute(moment: datetime) -> str:
        return moment.strftime(MINUTE_FORMAT)


# --- status for the admin page ------------------------------------------------------------------


@dataclass(frozen=True)
class ScannerStatus:
    enabled: bool
    paused: bool
    budget_per_min: int
    cursor_id: int | None
    head_id: int | None
    backlog: int | None
    created_per_min_15: float | None  # new match ids per minute, from the head ids seen
    created_per_min_60: float | None
    scanned_per_min_15: float | None
    scanned_per_min_60: float | None
    requests_per_min_15: float | None
    requests_per_min_60: float | None
    not_found_rate_60: float | None  # share of scanned ids that were deleted or private
    errors_60: int  # ids given up on after repeated failures, last hour
    open_lobbies: int
    gaps: list[Gap]
    last_error: str | None
    falling_behind: bool
    eta_minutes: float | None  # time to catch up, when the scanner is gaining on the head


def scanner_status(conn: sqlite3.Connection, settings: Settings, now: datetime) -> ScannerStatus:
    cursor = _int_cursor(conn, CURSOR_JOB)
    head = _int_cursor(conn, HEAD_JOB)
    backlog = head - cursor if head is not None and cursor is not None else None
    minute_now = now.strftime(MINUTE_FORMAT)
    rows = scan_repo.stats_since(conn, _minute_before(minute_now, LONG_WINDOW_MINUTES - 1))
    short = _window(rows, minute_now, SHORT_WINDOW_MINUTES)
    long = _window(rows, minute_now, LONG_WINDOW_MINUTES)
    created_15 = _created_per_min(short.rows)
    scanned_15 = short.per_min("ids_scanned")
    falling_behind = bool(
        backlog is not None
        and backlog >= CAUGHT_UP_BACKLOG
        and created_15 is not None
        and scanned_15 is not None
        and created_15 >= scanned_15
    )
    gaining = created_15 is not None and scanned_15 is not None and scanned_15 > created_15
    eta = backlog / (scanned_15 - created_15) if gaining and backlog and scanned_15 else None
    scanned_60 = sum(row.ids_scanned for row in long.rows)
    return ScannerStatus(
        enabled=settings.scanner_enabled,
        paused=Skimmer.is_paused(conn),
        budget_per_min=settings.scanner_budget_per_min,
        cursor_id=cursor,
        head_id=head,
        backlog=backlog,
        created_per_min_15=created_15,
        created_per_min_60=_created_per_min(long.rows),
        scanned_per_min_15=scanned_15,
        scanned_per_min_60=long.per_min("ids_scanned"),
        requests_per_min_15=short.per_min("requests"),
        requests_per_min_60=long.per_min("requests"),
        not_found_rate_60=(
            sum(row.not_found for row in long.rows) / scanned_60 if scanned_60 else None
        ),
        errors_60=sum(row.errors for row in long.rows),
        open_lobbies=scan_repo.count_open(conn),
        gaps=scan_repo.list_gaps(conn),
        last_error=_last_error(conn),
        falling_behind=falling_behind,
        eta_minutes=eta,
    )


@dataclass(frozen=True)
class _Window:
    rows: list[StatRow]
    minutes: int  # how long the window is, shortened when the counters are younger than it

    def per_min(self, column: str) -> float | None:
        if not self.rows:
            return None
        return sum(getattr(row, column) for row in self.rows) / self.minutes


def _window(rows: list[StatRow], minute_now: str, length: int) -> _Window:
    first = _minute_before(minute_now, length - 1)
    inside = [row for row in rows if row.minute >= first]
    if not inside:
        return _Window([], length)
    covered = _minutes_between(inside[0].minute, minute_now) + 1
    return _Window(inside, max(1, min(length, covered)))


def _created_per_min(rows: list[StatRow]) -> float | None:
    """New ids per minute: how far the head id moved between the first and last minute that
    recorded it."""
    seen = [row for row in rows if row.head_id is not None]
    if len(seen) < 2:
        return None
    first, last = seen[0], seen[-1]
    elapsed = _minutes_between(first.minute, last.minute)
    if elapsed <= 0 or first.head_id is None or last.head_id is None:
        return None
    return (last.head_id - first.head_id) / elapsed


def _minute_before(minute: str, minutes: int) -> str:
    moment = datetime.strptime(minute, MINUTE_FORMAT)
    return (moment - timedelta(minutes=minutes)).strftime(MINUTE_FORMAT)


def _minutes_between(first: str, last: str) -> int:
    delta = datetime.strptime(last, MINUTE_FORMAT) - datetime.strptime(first, MINUTE_FORMAT)
    return int(delta.total_seconds() // 60)


def _int_cursor(conn: sqlite3.Connection, name: str) -> int | None:
    state = jobs_repo.get(conn, name)
    if state is None or not state.cursor or not state.cursor.isdecimal():
        return None
    return int(state.cursor)


def _last_error(conn: sqlite3.Connection) -> str | None:
    lobby_error = app_settings_repo.get(conn, LAST_ERROR_KEY)
    if lobby_error:
        return lobby_error
    for name in (HEAD_JOB, "skimmer_workers", "skimmer_open_check"):
        state = jobs_repo.get(conn, name)
        if state is not None and state.last_error:
            return state.last_error
    return None
