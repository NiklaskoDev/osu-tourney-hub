"""The lobby scanner: first run, cursor, workers, gaps, open lobbies, switches and counters."""

import asyncio
import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.clients.osu import OsuUnavailable
from app.domain.models import FetchedMatch, MatchSummary
from app.repos import app_settings as app_settings_repo
from app.repos import jobs as jobs_repo
from app.repos import scan as scan_repo
from app.services.skimmer import (
    BACKFILL_MAX_LIVE_BACKLOG,
    LAST_ERROR_KEY,
    LONG_LOBBY,
    LONG_LOBBY_STEP,
    MAX_ATTEMPTS,
    IdDispenser,
    Skimmer,
)
from tests.support import add_member, add_pool_map, add_stage, add_team, add_tournament

pytestmark = pytest.mark.anyio

OURS = 2001
NM1 = 1001
CLOSED = "2026-11-12T11:00:00+00:00"
T0 = datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC)


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup")
    add_member(db, tid, OURS, "captain")
    add_team(db, tid, "Ours", [(OURS, "Captain")], ours=True)
    stage = add_stage(db, tid)
    add_pool_map(db, stage, "NM1", beatmap_id=NM1)
    return {"id": tid, "map": 1}


@pytest.fixture
def make(settings, clock, scan_client):
    def build(**changes) -> Skimmer:
        return Skimmer(dataclasses.replace(settings, **changes), clock, scan_client)

    return build


@pytest.fixture
def skimmer(make) -> Skimmer:
    return make()


def lobbies(osu, first, last, end_time=CLOSED):
    for match_id in range(first, last + 1):
        osu.add_match(match_id, f"room {match_id}", end_time=end_time)


def saved_cursor(db):
    state = jobs_repo.get(db, "skimmer")
    return int(state.cursor) if state and state.cursor else None


def saved_head(db):
    return int(jobs_repo.get(db, "skimmer_head").cursor)


async def drain(skimmer, db):
    """Run the workers' loop body until nothing is waiting. Returns how many ids it took."""
    taken = 0
    while await skimmer.work_one(db):
        taken += 1
    return taken


async def settle_retries(skimmer, db, clock):
    """Like `drain`, but waits out the pause before each retry until nothing is left."""
    for _ in range(10):
        await drain(skimmer, db)
        if not skimmer._dispenser._retry:
            return
        clock.current += timedelta(minutes=5)
    raise AssertionError("an id is still being retried")


def stat_total(db, column):
    return db.execute(f"SELECT COALESCE(SUM({column}), 0) FROM scan_stats").fetchone()[0]  # noqa: S608


def match_ids_fetched(osu):
    return sorted(
        {
            int(r.url.path.rsplit("/", 1)[1])
            for r in osu.requests
            if r.url.path.startswith("/api/v2/matches/")
        }
    )


# --- handing out ids ---


def test_ids_are_handed_out_in_ascending_order_up_to_the_head():
    dispenser = IdDispenser(cursor=10, head=13)

    assert [dispenser.take(T0) for _ in range(4)] == [11, 12, 13, None]

    dispenser.raise_head(15)
    assert [dispenser.take(T0) for _ in range(3)] == [14, 15, None]


def test_the_cursor_only_moves_over_a_contiguous_run_of_done_ids():
    dispenser = IdDispenser(cursor=10, head=20)
    taken = [dispenser.take(T0) for _ in range(4)]  # 11 to 14 are being worked on

    dispenser.done(13)
    dispenser.done(12)
    dispenser.done(14)
    assert dispenser.cursor == 10  # 11 is still open

    dispenser.done(11)
    assert dispenser.cursor == 14
    assert taken == [11, 12, 13, 14]


def test_a_failed_id_is_handed_out_again_before_new_ones_once_it_is_due():
    dispenser = IdDispenser(cursor=10, head=20)
    first, second = dispenser.take(T0), dispenser.take(T0)

    dispenser.retry(first, T0)

    assert (first, second, dispenser.take(T0), dispenser.take(T0)) == (11, 12, 11, 13)


def test_a_failed_id_waits_for_its_time_while_new_ids_go_on():
    dispenser = IdDispenser(cursor=10, head=13)
    dispenser.take(T0)
    dispenser.retry(11, T0 + timedelta(seconds=30))

    assert dispenser.take(T0 + timedelta(seconds=29)) == 12  # not due: a new id instead
    assert dispenser.take(T0 + timedelta(seconds=29)) == 13
    assert dispenser.take(T0 + timedelta(seconds=29)) is None
    assert dispenser.take(T0 + timedelta(seconds=30)) == 11


def test_due_retries_come_out_oldest_first():
    dispenser = IdDispenser(cursor=10, head=10)
    dispenser.retry(7, T0 + timedelta(seconds=5))
    dispenser.retry(8, T0)
    dispenser.retry(9, T0 + timedelta(seconds=5))

    later = T0 + timedelta(seconds=10)
    assert [dispenser.take(later) for _ in range(3)] == [7, 8, 9]


def test_skipping_ahead_drops_everything_up_to_the_new_cursor():
    dispenser = IdDispenser(cursor=10, head=100)
    dispenser.take(T0)
    dispenser.retry(11, T0)
    dispenser.done(60)

    dispenser.skip_to(50)

    assert dispenser.cursor == 50
    assert dispenser.take(T0) == 51  # the retry of 11 is gone
    dispenser.done(30)  # a worker finishing an id below the cursor changes nothing
    assert dispenser.cursor == 50
    assert dispenser.backlog == 50


def test_a_done_id_beyond_a_skip_still_counts_after_it():
    dispenser = IdDispenser(cursor=10, head=100)
    dispenser.done(51)

    dispenser.skip_to(50)

    assert dispenser.cursor == 51


# --- the first run and the head ---


async def test_the_first_run_starts_at_the_head_and_never_reads_history(db, osu, skimmer, cup):
    lobbies(osu, 1, 100)

    await skimmer.check_head(db)
    taken = await drain(skimmer, db)

    assert (saved_cursor(db), saved_head(db), taken) == (100, 100, 0)
    assert match_ids_fetched(osu) == []

    lobbies(osu, 101, 103)
    await skimmer.check_head(db)
    assert await drain(skimmer, db) == 3
    assert match_ids_fetched(osu) == [101, 102, 103]
    skimmer.flush_cursor(db)
    assert saved_cursor(db) == 103


async def test_a_restart_carries_on_after_the_saved_cursor(db, osu, make, cup):
    lobbies(osu, 1, 110)
    jobs_repo.set_cursor(db, "skimmer", "105")
    restarted = make()

    await restarted.check_head(db)
    await drain(restarted, db)

    assert match_ids_fetched(osu) == [106, 107, 108, 109, 110]
    restarted.flush_cursor(db)
    assert saved_cursor(db) == 110


async def test_the_cursor_is_only_written_when_it_moved(db, osu, skimmer, cup, monkeypatch):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    writes = []
    monkeypatch.setattr(jobs_repo, "set_cursor", lambda *args: writes.append(args))

    skimmer.flush_cursor(db)
    skimmer.flush_cursor(db)

    assert writes == []  # check_head already saved it


async def test_the_head_run_counts_its_requests_and_records_the_head(db, osu, skimmer, cup):
    lobbies(osu, 1, 10)

    await skimmer.check_head(db)

    assert stat_total(db, "requests") == 1
    assert db.execute("SELECT head_id FROM scan_stats").fetchone()[0] == 10


async def test_the_head_reads_up_to_three_pages_down_to_the_cursor(db, osu, skimmer, cup):
    lobbies(osu, 1, 400)
    jobs_repo.set_cursor(db, "skimmer", "100")

    await skimmer.check_head(db)

    listings = [r for r in osu.requests if r.url.path == "/api/v2/matches"]
    assert len(listings) == 3  # 150 ids, then it stops although the cursor is lower still


async def test_the_head_stops_reading_pages_once_it_reaches_the_cursor(db, osu, skimmer, cup):
    lobbies(osu, 1, 400)
    jobs_repo.set_cursor(db, "skimmer", "380")

    await skimmer.check_head(db)

    assert len([r for r in osu.requests if r.url.path == "/api/v2/matches"]) == 1


# --- what a worker does with an id ---


async def test_a_lobby_with_a_pool_map_is_kept_whatever_its_name(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, "just practice")
    osu.add_game(6, NM1, [(OURS, 900_000)])
    osu.add_match(7, "ranked fun")
    osu.add_game(7, 424242, [(OURS, 900_000)])
    await skimmer.check_head(db)

    await drain(skimmer, db)

    assert [tuple(r) for r in db.execute("SELECT mp_match_id, name FROM lobbies")] == [
        (6, "just practice")
    ]
    assert stat_total(db, "ids_scanned") == 2
    assert stat_total(db, "matches_with_hits") == 1


async def test_deleted_and_private_lobbies_are_counted_and_skipped(db, osu, skimmer, cup):
    lobbies(osu, 1, 3)
    await skimmer.check_head(db)
    lobbies(osu, 4, 6)
    osu.match_statuses[4] = 404
    osu.match_statuses[5] = 403
    await skimmer.check_head(db)

    await drain(skimmer, db)

    skimmer.flush_cursor(db)
    assert saved_cursor(db) == 6
    assert (stat_total(db, "ids_scanned"), stat_total(db, "not_found")) == (3, 2)
    assert len(osu.match_requests(4)) == 1  # not retried


async def test_an_id_that_keeps_failing_is_tried_three_times_then_counted_and_skipped(
    db, osu, skimmer, cup, clock
):
    lobbies(osu, 1, 3)
    await skimmer.check_head(db)
    lobbies(osu, 4, 5)
    osu.match_statuses[4] = 500
    await skimmer.check_head(db)

    await settle_retries(skimmer, db, clock)

    assert len(osu.match_requests(4)) == MAX_ATTEMPTS * 5  # three tries of five requests each
    skimmer.flush_cursor(db)
    assert saved_cursor(db) == 5
    assert stat_total(db, "ids_scanned") == 2
    assert "Lobby 4" in app_settings_repo.get(db, LAST_ERROR_KEY)
    assert "3 tries" in app_settings_repo.get(db, LAST_ERROR_KEY)


async def test_an_id_that_fails_once_is_read_on_the_next_try(db, osu, skimmer, cup, clock):
    lobbies(osu, 1, 3)
    await skimmer.check_head(db)
    osu.add_match(4)
    osu.add_game(4, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)
    osu.match_statuses[4] = 500
    assert await skimmer.work_one(db) is True  # fails
    del osu.match_statuses[4]

    clock.current += timedelta(seconds=30)
    await drain(skimmer, db)

    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 1
    assert app_settings_repo.get(db, LAST_ERROR_KEY) is None


async def test_the_workers_never_read_an_id_twice_in_a_normal_run(db, osu, skimmer, cup):
    lobbies(osu, 1, 3)
    await skimmer.check_head(db)
    lobbies(osu, 4, 30)
    await skimmer.check_head(db)

    await drain(skimmer, db)

    assert [len(osu.match_requests(i)) for i in range(4, 31)] == [1] * 27


# --- falling behind ---


async def test_a_backlog_beyond_the_limit_skips_ahead_and_records_the_exact_range(
    db, osu, make, cup
):
    skimmer = make(scanner_max_backlog=50)
    lobbies(osu, 1, 100)
    jobs_repo.set_cursor(db, "skimmer", "10")

    await skimmer.check_head(db)

    assert [tuple(r) for r in db.execute("SELECT start_id, end_id, reason FROM scan_gaps")] == [
        (11, 50, "skipped_backlog")
    ]
    assert saved_cursor(db) == 50
    await drain(skimmer, db)
    assert match_ids_fetched(osu) == list(range(51, 101))


async def test_a_backlog_inside_the_limit_creates_no_gap(db, osu, make, cup):
    skimmer = make(scanner_max_backlog=90)
    lobbies(osu, 1, 100)
    jobs_repo.set_cursor(db, "skimmer", "10")

    await skimmer.check_head(db)
    await skimmer.check_head(db)

    assert db.execute("SELECT COUNT(*) FROM scan_gaps").fetchone()[0] == 0


async def test_skipping_ahead_once_makes_one_gap_not_one_per_head_run(db, osu, make, cup):
    skimmer = make(scanner_max_backlog=50)
    lobbies(osu, 1, 100)
    jobs_repo.set_cursor(db, "skimmer", "10")

    for _ in range(3):
        await skimmer.check_head(db)

    assert db.execute("SELECT COUNT(*) FROM scan_gaps").fetchone()[0] == 1


async def seeded_gap(db, osu, make, size=5):
    skimmer = make(scanner_max_backlog=20)
    lobbies(osu, 1, 40)
    jobs_repo.set_cursor(db, "skimmer", str(40 - 20 - size))
    await skimmer.check_head(db)
    (gap,) = scan_repo.list_gaps(db)
    assert gap.size == size
    return skimmer, gap


async def test_a_backfill_reads_the_gap_oldest_first_and_records_its_progress(db, osu, make, cup):
    skimmer, gap = await seeded_gap(db, osu, make)
    await drain(skimmer, db)
    osu.requests.clear()

    assert scan_repo.request_backfill(db, gap.id) is True
    reads = 0
    while await skimmer.backfill_one(db):
        reads += 1
        progress = scan_repo.get_gap(db, gap.id).backfilled_until
        assert progress == gap.start_id - 1 + reads

    assert reads == gap.size
    assert match_ids_fetched(osu) == list(range(gap.start_id, gap.end_id + 1))
    done = scan_repo.get_gap(db, gap.id)
    assert done.backfill_done and done.backfilled_until == gap.end_id


async def test_nothing_is_backfilled_until_an_admin_asks(db, osu, make, cup):
    skimmer, gap = await seeded_gap(db, osu, make)
    await drain(skimmer, db)

    assert await skimmer.backfill_one(db) is False
    assert scan_repo.get_gap(db, gap.id).backfilled_until is None


async def test_a_backfill_is_asked_for_once(db, osu, make, cup):
    _, gap = await seeded_gap(db, osu, make)

    assert scan_repo.request_backfill(db, gap.id) is True
    assert scan_repo.request_backfill(db, gap.id) is False
    assert scan_repo.get_gap(db, gap.id).backfilled_until == gap.start_id - 1


async def test_a_backfill_never_runs_while_the_live_backlog_is_100_or_more(db, osu, make, cup):
    skimmer = make(scanner_max_backlog=500)
    lobbies(osu, 1, 300)
    jobs_repo.set_cursor(db, "skimmer", "100")
    await skimmer.check_head(db)  # a live backlog of 200
    gap = scan_repo.insert_gap(db, 1, 5, "admin_skip", "2026-11-12T00:00:00Z")
    scan_repo.request_backfill(db, gap)

    assert await skimmer.backfill_one(db) is False
    assert match_ids_fetched(osu) == []

    while skimmer._dispenser.backlog >= BACKFILL_MAX_LIVE_BACKLOG:
        assert await skimmer.work_one(db)
    assert await skimmer.backfill_one(db) is True
    assert 1 in match_ids_fetched(osu)


async def test_backfill_goes_to_the_oldest_requested_gap_first(db, osu, make, cup):
    skimmer = make()
    lobbies(osu, 1, 30)
    await skimmer.check_head(db)  # cursor at 30, nothing to scan
    later = scan_repo.insert_gap(db, 20, 21, "admin_skip", "2026-11-12T00:00:00Z")
    earlier = scan_repo.insert_gap(db, 5, 6, "admin_skip", "2026-11-12T00:00:00Z")
    scan_repo.request_backfill(db, later)
    scan_repo.request_backfill(db, earlier)

    while await skimmer.backfill_one(db):
        pass

    ids = [int(r.url.path.rsplit("/", 1)[1]) for r in osu.requests if "/matches/" in r.url.path]
    assert ids == [5, 6, 20, 21]


# --- lobbies that are still open ---


async def test_a_lobby_seen_open_in_the_listing_is_not_read_by_the_worker(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    osu.add_game(6, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)

    await drain(skimmer, db)

    assert osu.match_requests(6) == []
    (row,) = scan_repo.list_open(db)
    assert (row.mp_match_id, row.last_event_id, row.events_fetched) == (6, None, 0)
    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 0


async def test_once_it_closed_an_open_lobby_is_read_after_its_last_event(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    first = osu.add_game(6, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)
    skimmer._known_open.clear()  # as if the listing had not shown it open: the worker reads it
    await drain(skimmer, db)
    (row,) = scan_repo.list_open(db)
    assert row.last_event_id == first["id"]  # the game was read, the lobby stored half-done
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 0
    osu.add_game(6, NM1, [(OURS, 910_000)])
    osu.matches[6]["match"]["end_time"] = CLOSED
    osu.requests.clear()

    await skimmer.check_open(db)

    final = osu.match_requests(6)
    assert [r.url.params.get("after") for r in final] == [str(first["id"])]
    assert db.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 1
    assert scan_repo.list_open(db) == []


async def test_a_lobby_still_listed_as_active_is_left_alone(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    await skimmer.check_head(db)
    await drain(skimmer, db)
    osu.requests.clear()

    await skimmer.check_open(db)

    assert osu.match_requests(6) == []
    assert len(scan_repo.list_open(db)) == 1


async def test_nothing_is_asked_of_osu_when_no_lobby_is_waiting(db, osu, skimmer, cup):
    await skimmer.check_open(db)

    assert osu.requests == []


async def test_a_lobby_open_past_the_limit_is_finalised_even_though_still_active(
    db, osu, clock, make, cup
):
    skimmer = make(scanner_open_max_hours=24)
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    osu.add_game(6, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)
    await drain(skimmer, db)
    clock.current += timedelta(hours=24)

    await skimmer.check_open(db)

    assert scan_repo.list_open(db) == []
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 1


async def test_a_lobby_open_for_hours_is_read_in_steps_every_half_hour(db, osu, clock, make, cup):
    skimmer = make()
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    first = osu.add_game(6, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)
    skimmer._known_open.clear()
    await drain(skimmer, db)
    osu.requests.clear()

    clock.current += LONG_LOBBY - timedelta(minutes=1)
    await skimmer.check_open(db)
    assert osu.match_requests(6) == []  # not long enough yet

    clock.current += timedelta(minutes=2)
    second = osu.add_game(6, NM1, [(OURS, 910_000)])
    await skimmer.check_open(db)
    assert [r.url.params.get("after") for r in osu.match_requests(6)] == [str(first["id"])]
    (row,) = scan_repo.list_open(db)  # kept: the lobby is still open
    assert row.last_event_id == second["id"]
    assert db.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 2
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 0

    clock.current += LONG_LOBBY_STEP - timedelta(minutes=1)
    await skimmer.check_open(db)
    assert len(osu.match_requests(6)) == 1  # next step not due

    clock.current += timedelta(minutes=1)
    await skimmer.check_open(db)
    assert len(osu.match_requests(6)) == 2


async def test_a_lobby_listed_closed_but_still_open_when_read_is_kept(db, osu, clock, make, cup):
    skimmer = make()
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    await skimmer.check_head(db)
    await drain(skimmer, db)
    scan_repo.upsert_open(db, 6, "2026-11-12T12:30:15Z", None, 0, "2026-11-12T12:30:15Z")
    # The listing is cut off after 20 pages; here it simply omits the lobby.
    skimmer._osu.list_matches = _listing_without_active_lobbies(skimmer._osu)

    await skimmer.check_open(db)

    assert len(osu.match_requests(6)) == 1
    (row,) = scan_repo.list_open(db)
    assert row.next_check_at == "2026-11-12T13:00:15Z"  # looked at again in half an hour
    await skimmer.check_open(db)
    assert len(osu.match_requests(6)) == 1


def _listing_without_active_lobbies(client):
    original = client.list_matches

    async def listing(cursor_string=None, active=None, priority=None):
        if active:
            return [], None
        return await original(cursor_string, active)

    return listing


async def test_a_game_in_progress_is_read_again_once_it_is_over(db, osu, clock, make, cup):
    skimmer = make()
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    done = osu.add_game(6, 424242, [(OURS, 1)])
    running = osu.add_game(6, NM1, ended=False)
    await skimmer.check_head(db)
    skimmer._known_open.clear()
    await drain(skimmer, db)
    (row,) = scan_repo.list_open(db)
    assert row.last_event_id == done["id"]  # not the running game's event
    osu.finish_game(6, running, [(OURS, 920_000)])
    osu.matches[6]["match"]["end_time"] = CLOSED

    await skimmer.check_open(db)

    assert [tuple(r) for r in db.execute("SELECT osu_id, score FROM scores")] == [(OURS, 920_000)]


async def test_a_closed_lobby_that_vanished_is_forgotten(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    await skimmer.check_head(db)
    await drain(skimmer, db)
    osu.match_statuses[6] = 404
    del osu.matches[6]

    await skimmer.check_open(db)

    assert scan_repo.list_open(db) == []


async def test_a_lobby_osu_cannot_deliver_stays_for_the_next_run(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=CLOSED)
    scan_repo.upsert_open(db, 6, "2026-11-12T12:30:15Z", None, 0, "2026-11-12T12:30:15Z")
    osu.match_statuses[6] = 500

    await skimmer.check_open(db)

    assert len(scan_repo.list_open(db)) == 1


# --- pause, resume and the switch ---


async def test_a_paused_scanner_does_nothing_and_carries_on_after_resuming(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    lobbies(osu, 6, 8)
    Skimmer.set_paused(db, True)
    osu.requests.clear()

    await skimmer.check_head(db)
    assert await skimmer.work_one(db) is False
    assert await skimmer.backfill_one(db) is False
    await skimmer.check_open(db)
    assert osu.requests == []

    Skimmer.set_paused(db, False)
    await skimmer.check_head(db)
    assert await drain(skimmer, db) == 3
    assert match_ids_fetched(osu) == [6, 7, 8]


async def test_a_pause_lets_the_id_in_hand_finish_and_stops_the_next(db, osu, skimmer, cup):
    lobbies(osu, 1, 3)
    await skimmer.check_head(db)
    lobbies(osu, 4, 6)
    await skimmer.check_head(db)

    assert await skimmer.work_one(db)
    Skimmer.set_paused(db, True)
    assert await skimmer.work_one(db) is False

    assert match_ids_fetched(osu) == [4]


# --- the workers running together ---


class GatedOsu:
    """An osu! stand-in whose lobby reads wait for the test to let them through, so ids finish
    in any order the test wants."""

    scan_requests = 0

    def __init__(self, head):
        self.head = head
        self.gates = {}
        self.started = []
        self.finished = []

    def gate(self, match_id):
        return self.gates.setdefault(match_id, asyncio.Event())

    async def list_matches(self, cursor_string=None, active=None, priority=None):
        return [MatchSummary(self.head, "room", None, CLOSED)], None

    async def fetch_match(self, match_id, max_pages=10, priority=None):
        self.started.append(match_id)
        await self.gate(match_id).wait()
        self.finished.append(match_id)
        return FetchedMatch(MatchSummary(match_id, "room", None, CLOSED), [], False)


async def settle(condition, steps=500):
    for _ in range(steps):
        if condition():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the workers did not get there")


async def test_four_workers_finishing_out_of_order_only_move_the_cursor_over_done_ids(
    db, settings, clock
):
    gated = GatedOsu(head=20)
    jobs_repo.set_cursor(db, "skimmer", "10")

    async def quick(seconds):
        await asyncio.sleep(0.001)

    skimmer = Skimmer(dataclasses.replace(settings, scanner_workers=4), clock, gated, sleep=quick)
    await skimmer.check_head(db)
    run = asyncio.create_task(skimmer.run_workers())
    try:
        await settle(lambda: gated.started == [11, 12, 13, 14])
        for match_id in (12, 13, 14):
            gated.gate(match_id).set()
        await settle(lambda: {12, 13, 14} <= set(gated.finished))
        await settle(lambda: len(gated.started) >= 7)  # the free workers went on to 15, 16, 17
        await asyncio.sleep(0.05)  # long enough for the cursor to be saved a few times
        assert saved_cursor(db) == 10  # 11 is still being read

        gated.gate(11).set()
        await settle(lambda: saved_cursor(db) is not None and saved_cursor(db) >= 14)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    assert saved_cursor(db) >= 14


async def test_the_cursor_is_saved_when_the_workers_are_stopped(db, settings, clock):
    gated = GatedOsu(head=14)
    jobs_repo.set_cursor(db, "skimmer", "10")
    for match_id in (11, 12, 13, 14):
        gated.gate(match_id).set()

    async def never_flushes(seconds):
        await asyncio.sleep(0.001 if seconds < 5 else 3600)  # the 10 s flusher does not run

    skimmer = Skimmer(settings, clock, gated, sleep=never_flushes)
    await skimmer.check_head(db)
    run = asyncio.create_task(skimmer.run_workers())
    await settle(lambda: sorted(gated.finished) == [11, 12, 13, 14])
    await settle(lambda: skimmer._dispenser.cursor == 14)
    assert saved_cursor(db) == 10

    run.cancel()
    await asyncio.gather(run, return_exceptions=True)

    assert saved_cursor(db) == 14


async def test_an_error_in_one_id_does_not_stop_the_others(db, settings, clock):
    class Broken(GatedOsu):
        async def fetch_match(self, match_id, max_pages=10, priority=None):
            if match_id == 12:
                raise OsuUnavailable("down")
            return await super().fetch_match(match_id, max_pages, priority)

    broken = Broken(head=14)
    for match_id in (11, 13, 14):
        broken.gate(match_id).set()
    jobs_repo.set_cursor(db, "skimmer", "10")
    skimmer = Skimmer(settings, clock, broken)
    await skimmer.check_head(db)

    await settle_retries(skimmer, db, clock)

    skimmer.flush_cursor(db)
    assert saved_cursor(db) == 14  # 12 was given up after three tries
    assert "Lobby 12" in app_settings_repo.get(db, LAST_ERROR_KEY)


# --- a closing read without a new pool game ---


async def test_a_lobby_that_closes_after_a_game_on_another_map_is_finished(db, osu, skimmer, cup):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    first = osu.add_game(6, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)
    skimmer._known_open.clear()
    await drain(skimmer, db)
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 0
    osu.add_game(6, 424242, [(OURS, 1)])
    osu.matches[6]["match"]["end_time"] = CLOSED

    await skimmer.check_open(db)

    assert [r.url.params.get("after") for r in osu.match_requests(6)][-1] == str(first["id"])
    assert [tuple(r) for r in db.execute("SELECT is_complete, ended_at FROM lobbies")] == [
        (1, "2026-11-12T11:00:00Z")
    ]
    assert scan_repo.list_open(db) == []


# --- a backfill stays out of the live counters ---


async def test_a_backfill_does_not_touch_the_live_counters(db, osu, make, cup):
    skimmer, gap = await seeded_gap(db, osu, make)
    osu.match_statuses[gap.start_id] = 404
    await drain(skimmer, db)
    before = [
        tuple(r)
        for r in db.execute(
            "SELECT minute, ids_scanned, not_found, matches_with_hits, requests FROM scan_stats"
        )
    ]
    scan_repo.request_backfill(db, gap.id)

    reads = 0
    while await skimmer.backfill_one(db):
        reads += 1

    assert reads == gap.size
    assert gap.size > 1 and len(osu.match_requests(gap.start_id)) == 1
    after = [
        tuple(r)
        for r in db.execute(
            "SELECT minute, ids_scanned, not_found, matches_with_hits, requests FROM scan_stats"
        )
    ]
    assert after == before
    assert skimmer._osu.scan_requests > before[0][4]  # the requests were really made


async def test_the_requests_of_a_backfill_do_not_land_in_the_next_live_minute(db, osu, make, cup):
    skimmer, gap = await seeded_gap(db, osu, make)
    await drain(skimmer, db)
    scan_repo.request_backfill(db, gap.id)
    while await skimmer.backfill_one(db):
        pass
    requests_before = stat_total(db, "requests")
    lobbies(osu, 41, 42)

    await skimmer.check_head(db)  # one listing request
    await drain(skimmer, db)  # two lobby reads

    assert stat_total(db, "requests") - requests_before == 3


async def test_a_failing_backfill_id_is_not_counted_as_a_live_error(db, osu, make, cup):
    skimmer, gap = await seeded_gap(db, osu, make, size=2)
    await drain(skimmer, db)
    scanned = stat_total(db, "ids_scanned")
    osu.match_statuses[gap.start_id] = 500
    scan_repo.request_backfill(db, gap.id)

    for _ in range(MAX_ATTEMPTS):
        await skimmer.backfill_one(db)

    assert stat_total(db, "ids_scanned") == scanned
    assert scan_repo.get_gap(db, gap.id).backfilled_until == gap.start_id  # given up on, moved on


# --- the cap on the listing of open lobbies ---


async def test_more_pages_of_open_lobbies_than_the_cap_are_logged(db, osu, skimmer, cup, caplog):
    for match_id in range(1, 1051):  # 21 pages of 50
        osu.add_match(match_id, end_time=None)
    scan_repo.upsert_open(db, 5000, "2026-11-12T12:00:00Z", None, 0, "2026-11-12T12:30:15Z")
    osu.add_match(5000, end_time=None)

    with caplog.at_level("WARNING"):
        await skimmer.check_open(db)

    assert "More than 20 pages of open lobbies" in caplog.text
    assert len([r for r in osu.requests if r.url.params.get("active")]) == 20


async def test_a_listing_inside_the_cap_logs_nothing(db, osu, skimmer, cup, caplog):
    for match_id in range(1, 1001):  # exactly 20 pages
        osu.add_match(match_id, end_time=None)
    scan_repo.upsert_open(db, 5, "2026-11-12T12:00:00Z", None, 0, "2026-11-12T12:30:15Z")

    with caplog.at_level("WARNING"):
        await skimmer.check_open(db)

    assert "pages of open lobbies" not in caplog.text


# --- lobbies remembered as open are forgotten once behind the cursor ---


async def test_ids_remembered_as_open_are_dropped_when_the_cursor_passes_them(db, osu, make, cup):
    skimmer = make(scanner_max_backlog=50)
    lobbies(osu, 1, 100)
    osu.add_match(10, end_time=None)
    osu.add_match(90, end_time=None)
    jobs_repo.set_cursor(db, "skimmer", "5")

    await skimmer.check_head(db)  # skips ahead to 50: id 10 is now behind the cursor

    assert skimmer._known_open == {90}


# --- failed ids: a pause before each retry, and a record when they are given up on ---


async def failing_id(db, osu, skimmer, last=5):
    lobbies(osu, 1, 3)
    await skimmer.check_head(db)
    lobbies(osu, 4, last)
    await skimmer.check_head(db)


async def test_a_failed_id_is_retried_after_30_seconds_times_the_failures_so_far(
    db, osu, skimmer, cup, clock
):
    await failing_id(db, osu, skimmer, last=4)
    osu.match_statuses[4] = 500

    assert await skimmer.work_one(db) is True  # first failure
    assert await skimmer.work_one(db) is False  # nothing else to do, 4 is not due
    clock.current += timedelta(seconds=29)
    assert await skimmer.work_one(db) is False
    clock.current += timedelta(seconds=1)
    assert await skimmer.work_one(db) is True  # second failure: now it waits 60 seconds
    clock.current += timedelta(seconds=59)
    assert await skimmer.work_one(db) is False
    clock.current += timedelta(seconds=1)
    assert await skimmer.work_one(db) is True  # the third failure gives up
    assert await skimmer.work_one(db) is False
    assert len(osu.match_requests(4)) == MAX_ATTEMPTS * 5


async def test_other_ids_are_read_while_a_failed_one_waits(db, osu, skimmer, cup, clock):
    await failing_id(db, osu, skimmer, last=6)
    osu.match_statuses[4] = 500

    assert await drain(skimmer, db) == 3  # 4 fails, 5 and 6 are read, 4 is not due yet

    assert len(osu.match_requests(5)) == 1 and len(osu.match_requests(6)) == 1
    assert len(osu.match_requests(4)) == 5  # one try so far (five requests)
    skimmer.flush_cursor(db)
    assert saved_cursor(db) == 3  # 4 is still open, so the cursor waits


async def test_an_id_given_up_on_becomes_a_range_that_can_be_backfilled(
    db, osu, skimmer, cup, clock
):
    await failing_id(db, osu, skimmer)
    osu.match_statuses[4] = 500

    await settle_retries(skimmer, db, clock)

    (gap,) = scan_repo.list_gaps(db)
    assert (gap.start_id, gap.end_id, gap.reason, gap.backfilled_until) == (
        4,
        4,
        "fetch_error",
        None,
    )
    assert stat_total(db, "errors") == 1
    skimmer.flush_cursor(db)
    assert saved_cursor(db) == 5

    del osu.match_statuses[4]
    osu.add_game(4, NM1, [(OURS, 900_000)])
    scan_repo.request_backfill(db, gap.id)
    while await skimmer.backfill_one(db):
        pass

    assert scan_repo.get_gap(db, gap.id).backfill_done
    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 1


async def test_adjacent_failed_ids_share_one_range_and_others_get_their_own(
    db, osu, skimmer, cup, clock
):
    await failing_id(db, osu, skimmer, last=9)
    for match_id in (4, 5, 6, 8):
        osu.match_statuses[match_id] = 500

    await settle_retries(skimmer, db, clock)

    ranges = [(g.start_id, g.end_id, g.reason) for g in scan_repo.list_gaps(db)]
    assert ranges == [(4, 6, "fetch_error"), (8, 8, "fetch_error")]
    assert stat_total(db, "errors") == 4


async def test_a_failure_while_backfilling_adds_no_range_and_no_error(db, osu, make, cup):
    skimmer, gap = await seeded_gap(db, osu, make, size=2)
    await drain(skimmer, db)
    osu.match_statuses[gap.start_id] = 500
    scan_repo.request_backfill(db, gap.id)

    for _ in range(MAX_ATTEMPTS):
        await skimmer.backfill_one(db)

    assert [g.reason for g in scan_repo.list_gaps(db)] == ["skipped_backlog"]
    assert stat_total(db, "errors") == 0


async def test_a_deleted_lobby_is_not_an_error(db, osu, skimmer, cup, clock):
    await failing_id(db, osu, skimmer, last=4)
    osu.match_statuses[4] = 404

    await settle_retries(skimmer, db, clock)

    assert scan_repo.list_gaps(db) == []
    assert stat_total(db, "errors") == 0


# --- a stored lobby that osu! no longer has ---


async def test_an_open_lobby_that_vanished_is_marked_complete_but_keeps_what_was_read(
    db, osu, skimmer, cup
):
    lobbies(osu, 1, 5)
    await skimmer.check_head(db)
    osu.add_match(6, end_time=None)
    osu.add_game(6, NM1, [(OURS, 900_000)])
    await skimmer.check_head(db)
    skimmer._known_open.clear()
    await drain(skimmer, db)
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 0
    del osu.matches[6]  # osu! no longer lists it, and reading it gives a 404

    await skimmer.check_open(db)

    assert scan_repo.list_open(db) == []
    assert db.execute("SELECT is_complete FROM lobbies").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 1


# --- starting from a lobby id (admin) ---

from app.repos.scan import Gap  # noqa: E402
from app.services.skimmer import (  # noqa: E402
    StartError,
    StartPlan,
    backfill_eta_minutes,
    plan_start,
    scanner_status,
    start_from,
)


def gap(gap_id, start, end, reason="skipped_backlog"):
    return Gap(gap_id, start, end, reason, "2026-11-12T00:00:00Z", None)


def plan(start, gaps=(), cursor=1000, head=1200):
    return plan_start(start, cursor, head, list(gaps))


@pytest.mark.parametrize(
    ("start", "kind", "first", "last", "size"),
    [
        (900, "backfill", 900, 1000, 101),
        (1000, "backfill", 1000, 1000, 1),
        (1, "backfill", 1, 1000, 1000),
        (1001, "nothing", 1001, 1000, 0),
        (1002, "skip", 1001, 1001, 1),
        (1100, "skip", 1001, 1099, 99),
        (1200, "skip", 1001, 1199, 199),
    ],
)
def test_a_start_id_becomes_a_backfill_a_skip_or_nothing(start, kind, first, last, size):
    found = plan(start)

    assert (found.kind, found.start_id, found.end_id, found.size) == (kind, first, last, size)
    assert found.merged_gap_ids == ()


def test_an_id_newer_than_the_head_is_refused_naming_the_head():
    with pytest.raises(StartError, match=r"1,200"):
        plan(1201)


@pytest.mark.parametrize("start", [0, -5])
def test_an_id_below_one_is_refused(start):
    with pytest.raises(StartError, match="Enter a lobby id or an osu! match link."):
        plan(start)


@pytest.mark.parametrize(("cursor", "head"), [(None, 1200), (1000, None), (None, None)])
def test_before_the_scanner_has_started_there_is_no_plan(cursor, head):
    with pytest.raises(StartError, match="has not started"):
        plan_start(900, cursor, head, [])


def test_a_backfill_replaces_the_skipped_ranges_it_overlaps():
    found = plan(900, [gap(1, 950, 960), gap(2, 880, 920), gap(3, 500, 600), gap(4, 850, 899)])

    assert (found.kind, found.start_id, found.end_id) == ("backfill", 880, 1000)
    assert found.merged_gap_ids == (1, 2)  # a range that ends before the start is not merged
    assert found.size == 121


def test_a_gap_that_straddles_the_start_extends_the_range_downwards_only():
    found = plan(900, [gap(1, 800, 905)])

    assert (found.start_id, found.end_id, found.merged_gap_ids) == (800, 1000, (1,))


def test_a_skip_ignores_the_listed_gaps():
    found = plan(1100, [gap(1, 100, 200)])

    assert (found.kind, found.merged_gap_ids) == ("skip", ())


async def test_applying_a_backfill_deletes_merged_gaps_and_requests_one_admin_range(db, skimmer):
    stale = scan_repo.insert_gap(db, 950, 960, "skipped_backlog", "2026-11-12T00:00:00Z")
    kept = scan_repo.insert_gap(db, 500, 600, "skipped_backlog", "2026-11-12T00:00:00Z")
    jobs_repo.set_cursor(db, "skimmer", "1000")

    new_id = start_from(db, skimmer, plan(900, scan_repo.list_gaps(db)), T0)

    assert scan_repo.get_gap(db, stale) is None and scan_repo.get_gap(db, kept) is not None
    created = scan_repo.get_gap(db, new_id)
    assert (created.start_id, created.end_id, created.reason) == (900, 1000, "admin_skip")
    assert created.backfilled_until == 899 and created.created_at == "2026-11-12T12:30:15Z"
    assert saved_cursor(db) == 1000  # the live cursor does not move back


async def test_applying_a_skip_records_a_range_and_moves_the_cursor_forward(db, osu, make, cup):
    skimmer = make()
    lobbies(osu, 1, 1200)
    jobs_repo.set_cursor(db, "skimmer", "1000")
    await skimmer.check_head(db)

    assert start_from(db, skimmer, plan(1100), T0) is None

    (created,) = scan_repo.list_gaps(db)
    assert (created.start_id, created.end_id, created.reason) == (1001, 1099, "admin_skip")
    assert created.backfilled_until is None  # listed, not requested
    assert saved_cursor(db) == 1099
    assert skimmer._dispenser.take(T0) == 1100


async def test_a_skip_forgets_open_lobbies_it_skipped(db, osu, make, cup):
    skimmer = make()
    lobbies(osu, 1, 1200, end_time=None)
    jobs_repo.set_cursor(db, "skimmer", "1000")
    await skimmer.check_head(db)
    assert 1055 in skimmer._known_open

    start_from(db, skimmer, plan(1100), T0)

    assert 1055 not in skimmer._known_open and 1150 in skimmer._known_open


async def test_applying_nothing_changes_nothing(db, skimmer):
    assert start_from(db, skimmer, plan(1001), T0) is None
    assert scan_repo.list_gaps(db) == []


async def test_skipping_before_the_first_head_check_is_refused_and_leaves_no_range(db, skimmer):
    with pytest.raises(StartError, match="has not started"):
        start_from(db, skimmer, StartPlan("skip", 1001, 1099, 99), T0)

    assert scan_repo.list_gaps(db) == []  # rolled back with the failed skip


async def test_a_skip_is_not_undone_by_the_next_head_check(db, osu, make, cup):
    skimmer = make(scanner_max_backlog=500)
    lobbies(osu, 1, 1200)
    jobs_repo.set_cursor(db, "skimmer", "1000")
    await skimmer.check_head(db)
    start_from(db, skimmer, plan(1100), T0)

    await skimmer.check_head(db)  # the head is 1200, 100 ahead: inside the allowed backlog

    assert [g.reason for g in scan_repo.list_gaps(db)] == ["admin_skip"]
    assert saved_cursor(db) == 1099


async def test_the_backfill_worker_reads_an_admin_range_oldest_first(db, osu, make, cup):
    skimmer = make()
    lobbies(osu, 1, 30)
    await skimmer.check_head(db)
    new_id = start_from(db, skimmer, plan_start(20, 30, 30, []), T0)
    osu.requests.clear()

    while await skimmer.backfill_one(db):
        pass

    assert match_ids_fetched(osu) == list(range(20, 31))
    assert scan_repo.get_gap(db, new_id).backfill_done


# --- stopping a backfill ---


def requested_gap(db, start, end, read_until):
    gap_id = scan_repo.insert_gap(db, start, end, "admin_skip", "2026-11-12T00:00:00Z")
    scan_repo.request_backfill(db, gap_id)
    scan_repo.set_backfilled_until(db, gap_id, read_until)
    return gap_id


def test_stopping_keeps_what_was_read_and_leaves_the_rest_to_backfill_again(db):
    gap_id = requested_gap(db, 100, 199, 149)

    assert scan_repo.stop_backfill(db, gap_id) is True

    stopped = scan_repo.get_gap(db, gap_id)
    assert (stopped.start_id, stopped.end_id, stopped.backfilled_until) == (150, 199, None)
    assert scan_repo.next_backfill(db) is None
    assert scan_repo.request_backfill(db, gap_id) is True


def test_stopping_before_anything_was_read_changes_only_the_request(db):
    gap_id = requested_gap(db, 100, 199, 99)

    assert scan_repo.stop_backfill(db, gap_id) is True

    stopped = scan_repo.get_gap(db, gap_id)
    assert (stopped.start_id, stopped.backfilled_until) == (100, None)


def test_a_gap_that_was_never_requested_or_is_finished_cannot_be_stopped(db):
    idle = scan_repo.insert_gap(db, 10, 20, "skipped_backlog", "2026-11-12T00:00:00Z")
    done = requested_gap(db, 30, 40, 40)

    assert scan_repo.stop_backfill(db, idle) is False
    assert scan_repo.stop_backfill(db, done) is False
    assert scan_repo.stop_backfill(db, 999) is False
    assert scan_repo.get_gap(db, done).backfilled_until == 40


def test_deleting_gaps_removes_only_the_given_ones(db):
    first = scan_repo.insert_gap(db, 1, 2, "skipped_backlog", "2026-11-12T00:00:00Z")
    second = scan_repo.insert_gap(db, 3, 4, "skipped_backlog", "2026-11-12T00:00:00Z")

    scan_repo.delete_gaps(db, [first])
    scan_repo.delete_gaps(db, [])

    assert [g.id for g in scan_repo.list_gaps(db)] == [second]


# --- how long a backfill takes ---


def eta_status(db, settings, clock, requests, budget):
    for ago in range(0, 60):
        scan_repo.add_stats(
            db,
            (clock.current - timedelta(minutes=ago)).strftime("%Y-%m-%dT%H:%M"),
            ids_scanned=1,
            requests=requests,
        )
    return scanner_status(
        db, dataclasses.replace(settings, scanner_budget_per_min=budget), clock.current
    )


def test_the_eta_divides_the_size_by_the_spare_budget(db, settings, clock):
    status = eta_status(db, settings, clock, requests=20, budget=60)

    assert backfill_eta_minutes(4000, status) == pytest.approx(4000 / 40)


def test_the_eta_is_unknown_when_no_budget_is_spare(db, settings, clock):
    status = eta_status(db, settings, clock, requests=30, budget=30)

    assert backfill_eta_minutes(4000, status) is None


# --- review fixes: a stop is not undone, a skip that did not move, one number format ---


def test_progress_is_only_recorded_while_the_backfill_is_requested_and_only_forwards(db):
    idle = scan_repo.insert_gap(db, 10, 20, "skipped_backlog", "2026-11-12T00:00:00Z")
    running = requested_gap(db, 100, 199, 99)

    scan_repo.set_backfilled_until(db, idle, 12)
    scan_repo.set_backfilled_until(db, running, 120)
    scan_repo.set_backfilled_until(db, running, 110)  # a late, older write

    assert scan_repo.get_gap(db, idle).backfilled_until is None
    assert scan_repo.get_gap(db, running).backfilled_until == 120


async def test_a_stop_that_lands_while_an_id_is_being_read_is_not_undone(db, osu, make, cup):
    skimmer = make()
    lobbies(osu, 1, 30)
    await skimmer.check_head(db)
    gap_id = requested_gap(db, 20, 30, 24)

    async def read_then_get_stopped(conn, match_id, live=True):
        assert scan_repo.stop_backfill(conn, gap_id)  # the admin clicks Stop during the fetch
        return True

    skimmer._scan_guarded = read_then_get_stopped

    assert await skimmer.backfill_one(db) is True

    stopped = scan_repo.get_gap(db, gap_id)
    assert (stopped.start_id, stopped.backfilled_until) == (25, None)
    assert scan_repo.next_backfill(db) is None  # it does not resume by itself


def test_skip_forward_says_whether_the_cursor_moved(db, skimmer):
    skimmer._dispenser = IdDispenser(1100, 1200)

    assert skimmer.skip_forward(db, 1100) is False
    assert skimmer.skip_forward(db, 1050) is False
    assert skimmer.skip_forward(db, 1150) is True
    assert skimmer._dispenser.cursor == 1150
    assert saved_cursor(db) == 1150


async def test_a_skip_to_where_the_live_cursor_already_is_stores_no_range(db, skimmer):
    jobs_repo.set_cursor(db, "skimmer", "1000")  # the saved cursor lags the live one
    skimmer._dispenser = IdDispenser(1100, 1200)

    with pytest.raises(StartError, match="already continues past lobby id 1,100"):
        start_from(db, skimmer, plan(1100), T0)

    assert scan_repo.list_gaps(db) == []
    assert saved_cursor(db) == 1000


def test_the_head_message_uses_one_number_format():
    with pytest.raises(StartError) as raised:
        plan(12_001, cursor=1000, head=12_000)

    assert str(raised.value).count("12,000") == 2 and "12000" not in str(raised.value)


# --- start from an id: ordering and finished ranges ---


async def test_a_failed_insert_leaves_memory_and_database_in_step(db, skimmer, monkeypatch):
    jobs_repo.set_cursor(db, "skimmer", "1000")
    skimmer._dispenser = IdDispenser(1000, 1200)

    def broken_insert(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(scan_repo, "insert_gap", broken_insert)

    with pytest.raises(sqlite3.OperationalError):
        start_from(db, skimmer, plan(1100), T0)

    assert skimmer.live_cursor == 1000  # the scanner did not skip
    assert saved_cursor(db) == 1000
    assert not db.in_transaction


def test_the_live_cursor_is_none_until_the_first_head_check(skimmer):
    assert skimmer.live_cursor is None
    skimmer._dispenser = IdDispenser(1100, 1200)
    assert skimmer.live_cursor == 1100


def test_a_finished_backfill_is_left_as_done_and_not_folded_into_a_new_range():
    done = Gap(1, 950, 960, "admin_skip", "2026-11-12T00:00:00Z", 960)
    running = Gap(2, 940, 970, "admin_skip", "2026-11-12T00:00:00Z", 955)
    unread = gap(3, 920, 930)

    found = plan(900, [done, running, unread])

    assert found.merged_gap_ids == (2, 3)
    assert (found.start_id, found.end_id) == (900, 1000)
