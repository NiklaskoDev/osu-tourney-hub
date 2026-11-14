"""Live match mode: the lobby of a match is read every few seconds while it is played and the
people watching the live page are told what changed.

`LiveHub` is the in-process fan-out (it assumes the single worker process the app runs with).
`LiveFollower` runs one task per followed match: it reads the lobby at the highest osu! priority,
keeps the match's events up to date, and publishes a fresh `LiveState` whenever it changed. It
stops when the lobby is closed, after `MAX_FOLLOW`, or when a captain stops it.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.clients.osu import OsuClient, OsuError, OsuNotFound
from app.clients.ratelimit import Priority
from app.clock import Clock, to_utc_iso
from app.config import Settings
from app.db.connection import open_db
from app.domain.models import Match, Tournament
from app.repos import matches as matches_repo
from app.repos import tournaments as tournaments_repo
from app.services import history, mp_parse
from app.services.live_state import FINISHED, RUNNING, STOPPED, WAITING, LiveState, build_state

logger = logging.getLogger(__name__)

AUTOSTART_BEFORE = timedelta(minutes=30)
MAX_FOLLOW = timedelta(hours=4)
MAX_FAILED_POLLS = 5  # in a row; the osu! client has already retried each one
SUBSCRIBER_QUEUE_SIZE = 8
NOTICE_NO_LOBBY = "This match has no multiplayer lobby linked any more."
NOTICE_LOBBY_GONE = "osu! has no lobby with this number. Check the link on wybin."
NOTICE_UNREACHABLE = "osu! did not answer several times in a row. Start live updates again later."
NOTICE_RETRYING = "osu! did not answer. Trying again."
NOTICE_TIMEOUT = "Live updates stopped after four hours."
NOTICE_ERROR = "Live updates stopped because of an error. See the server log."

Sleep = Callable[[float], Awaitable[None]]


class LiveHub:
    """Fan-out of live states to the connected pages, one queue per page. A page that reads too
    slowly loses its oldest states, which is harmless: every state is complete."""

    def __init__(self, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[int, list[asyncio.Queue[LiveState]]] = {}
        self._latest: dict[int, LiveState] = {}

    def subscribe(self, match_id: int) -> asyncio.Queue[LiveState]:
        queue: asyncio.Queue[LiveState] = asyncio.Queue(self._queue_size)
        self._subscribers.setdefault(match_id, []).append(queue)
        return queue

    def unsubscribe(self, match_id: int, queue: asyncio.Queue[LiveState]) -> None:
        """Drop a page that went away. Dropping one twice is fine."""
        queues = self._subscribers.get(match_id, [])
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(match_id, None)

    def publish(self, match_id: int, state: LiveState) -> int:
        """Give the state to every subscriber of the match; returns how many got it."""
        self._latest[match_id] = state
        queues = self._subscribers.get(match_id, [])
        for queue in queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(state)
        return len(queues)

    def latest(self, match_id: int) -> LiveState | None:
        return self._latest.get(match_id)

    def subscriber_count(self, match_id: int) -> int:
        return len(self._subscribers.get(match_id, []))


class LiveFollower:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        osu: OsuClient,
        hub: LiveHub | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.hub = hub or LiveHub()
        self._db_path: Path = settings.db_path
        self._poll_seconds = settings.live_poll_seconds
        self._clock = clock
        self._osu = osu
        self._sleep = sleep
        self._tasks: dict[int, asyncio.Task[None]] = {}
        # Matches that were followed to the end or stopped: the automatic start leaves them alone
        # until a captain starts them again.
        self._ended: set[int] = set()

    def is_following(self, match_id: int) -> bool:
        return match_id in self._tasks

    def start(self, match_id: int) -> bool:
        """Follow the match's lobby. Returns False when it is already followed. Must run inside
        the event loop."""
        if match_id in self._tasks:
            return False
        self._ended.discard(match_id)
        task = asyncio.create_task(self._run(match_id), name=f"live-{match_id}")
        task.add_done_callback(lambda done: self._done(match_id, done))
        self._tasks[match_id] = task
        return True

    async def stop(self, match_id: int) -> bool:
        """Stop following. Returns False when it was not followed."""
        task = self._tasks.get(match_id)
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def stop_all(self) -> None:
        for match_id in list(self._tasks):
            await self.stop(match_id)

    async def start_due(self) -> int:
        """Follow the unfinished matches of our team that start within `AUTOSTART_BEFORE` (or
        started less than `MAX_FOLLOW` ago), except those already followed or ended. Returns how
        many were started."""
        now = self._clock.now()
        with closing(open_db(self._db_path)) as conn:
            due = matches_repo.list_live_candidates(
                conn, to_utc_iso(now - MAX_FOLLOW), to_utc_iso(now + AUTOSTART_BEFORE)
            )
        return sum(1 for match in due if match.id not in self._ended and self.start(match.id))

    def current_state(
        self, conn: sqlite3.Connection, tournament: Tournament, match: Match
    ) -> LiveState:
        """What a page shows now: the last published state, else one built from the stored
        events."""
        latest = self.hub.latest(match.id)
        if latest is not None:
            return latest
        status = FINISHED if match.status == "done" else WAITING
        return build_state(conn, tournament, match, status)

    # --- the follow task ---------------------------------------------------------------------

    def _done(self, match_id: int, task: asyncio.Task[tuple[str, str | None]]) -> None:
        """Runs when a follow task ends for any reason, even one stopped before it began."""
        self._tasks.pop(match_id, None)
        self._ended.add(match_id)
        if task.cancelled():
            outcome: tuple[str, str | None] = (STOPPED, None)
        elif (error := task.exception()) is not None:
            logger.error("Live updates of match %s failed", match_id, exc_info=error)
            outcome = (STOPPED, NOTICE_ERROR)
        else:
            outcome = task.result()
        if outcome[0] != FINISHED:  # a finished follow published its last state already
            self._publish(match_id, *outcome)

    async def _run(self, match_id: int) -> tuple[str, str | None]:
        started = self._clock.now()
        lobby_events: dict[int, dict[str, Any]] = {}
        failures = 0
        first_read = True
        last: LiveState | None = None
        while True:
            loaded = self._load(match_id)
            if loaded is None:
                return STOPPED, NOTICE_NO_LOBBY
            match, tournament = loaded
            notice = None
            closed = False
            try:
                fetched = await self._read(match.mp_match_id, lobby_events, first_read)
            except OsuNotFound:
                return STOPPED, NOTICE_LOBBY_GONE
            except OsuError:
                failures += 1
                if failures >= MAX_FAILED_POLLS:
                    return STOPPED, NOTICE_UNREACHABLE
                notice = NOTICE_RETRYING
            else:
                failures, first_read = 0, False
                closed = fetched.match.end_time is not None
            state = self._store_and_build(
                match, tournament, lobby_events, FINISHED if closed else RUNNING, notice
            )
            if state != last:
                self.hub.publish(match_id, state)
                last = state
            if closed:
                return FINISHED, None
            if self._clock.now() - started >= MAX_FOLLOW:
                return STOPPED, NOTICE_TIMEOUT
            await self._sleep(self._poll_seconds)

    def _load(self, match_id: int, needs_lobby: bool = True) -> tuple[Match, Tournament] | None:
        with closing(open_db(self._db_path)) as conn:
            match = matches_repo.get(conn, match_id)
            if match is None or (needs_lobby and match.mp_match_id is None):
                return None
            tournament = tournaments_repo.get(conn, match.tournament_id)
            return (match, tournament) if tournament else None

    async def _read(self, lobby_id: int, lobby_events: dict[int, dict[str, Any]], first: bool):
        """The first read takes the whole lobby, later ones only what came after the last event
        that cannot change any more (a game being played is read again when it ends)."""
        if first:
            fetched = await self._osu.fetch_match(lobby_id, priority=Priority.LIVE)
        else:
            after = mp_parse.last_final_event_id(list(lobby_events.values())) or 0
            fetched = await self._osu.fetch_match_after(lobby_id, after, priority=Priority.LIVE)
        if fetched.truncated:
            logger.warning("Lobby %s has more events than were read", lobby_id)
        lobby_events.update({event["id"]: event for event in fetched.events})
        return fetched

    def _store_and_build(
        self,
        match: Match,
        tournament: Tournament,
        lobby_events: dict[int, dict[str, Any]],
        status: str,
        notice: str | None,
    ) -> LiveState:
        ordered = [lobby_events[key] for key in sorted(lobby_events)]
        with closing(open_db(self._db_path)) as conn:
            history.store_lobby_events(conn, match, ordered, self._clock.now())
            return build_state(conn, tournament, match, status, notice, _game_in_progress(ordered))

    def _publish(self, match_id: int, status: str, notice: str | None) -> None:
        """The last state of a follow task: why it ended."""
        try:
            loaded = self._load(match_id, needs_lobby=False)
            if loaded is None:
                return
            match, tournament = loaded
            with closing(open_db(self._db_path)) as conn:
                self.hub.publish(match_id, build_state(conn, tournament, match, status, notice))
        except Exception:
            logger.exception("Could not publish the end of live updates for match %s", match_id)


def _game_in_progress(ordered_events: list[dict[str, Any]]) -> int | None:
    """The beatmap of the newest game that has not ended yet."""
    for event in reversed(ordered_events):
        game = event.get("game")
        if isinstance(game, dict) and not game.get("end_time"):
            beatmap_id = game.get("beatmap_id")
            return beatmap_id if isinstance(beatmap_id, int) and beatmap_id > 0 else None
    return None
