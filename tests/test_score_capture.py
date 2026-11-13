"""Score capture: what gets queued, how the queue runs, and what is stored."""

import asyncio
import time
from datetime import timedelta

import pytest

from app.clients.osu import OsuUnavailable
from app.clients.ratelimit import Priority, RateLimiter
from app.domain.models import Stage
from app.repos import capture as capture_repo
from app.repos import tournaments as tournaments_repo
from app.services import score_capture as capture
from app.services.score_capture import MAX_ATTEMPTS, PRIORITY_AUTOMATIC, PRIORITY_REQUESTED
from tests.support import (
    add_member,
    add_pool_map,
    add_stage,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER_PLAYER, GUEST = 2001, 2002, 2003, 2004
DAY = timedelta(hours=24)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def cup(db):
    """An osu! tournament with a captain, two players and a guest, and three stages: the first
    ended long ago, the second is running (beatmaps 11 and 12), the third comes next (beatmaps 12
    and 13) and the fourth is further away (beatmap 14)."""
    tid = add_tournament(db)
    for osu_id, role in ((CAPTAIN, "captain"), (PLAYER, "player"), (GUEST, "guest")):
        add_member(db, tid, osu_id, role)
    ended = add_stage(db, tid, "Qualifiers", 1)
    db.execute("UPDATE stages SET end_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ended,))
    add_pool_map(db, ended, "NM1", beatmap_id=10)
    current = add_stage(db, tid, "Round of 16", 2)
    add_pool_map(db, current, "NM1", beatmap_id=11)
    add_pool_map(db, current, "HD1", beatmap_id=12, required_mods="HD")
    add_pool_map(db, current, "TB", beatmap_id=15, tiebreaker=True)
    db.execute("UPDATE pool_maps SET beatmap_id = NULL WHERE slot = 'TB'")  # not announced yet
    following = add_stage(db, tid, "Quarterfinals", 3)
    add_pool_map(db, following, "NM1", beatmap_id=12)  # also in the previous pool
    add_pool_map(db, following, "NM2", beatmap_id=13)
    far = add_stage(db, tid, "Semifinals", 4)
    add_pool_map(db, far, "NM1", beatmap_id=14)
    return tournaments_repo.get(db, tid)


def queued(db):
    rows = db.execute(
        "SELECT osu_id, beatmap_id, priority FROM capture_queue ORDER BY osu_id, beatmap_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def stored(db):
    rows = db.execute(
        "SELECT beatmap_id, osu_id, mods, score, scoring, accuracy, max_combo, misses, passed, "
        "achieved_at, source, source_ref, source_url, entered_by, tournament_id FROM scores "
        "ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


# --- which stages and maps are captured ---


def stage(name: str, end_at: str | None) -> Stage:
    return Stage(1, 1, None, name, 1, None, end_at, None, None, None, False, None, True)


def test_the_stage_being_played_and_the_next_one_are_captured(clock):
    stages = [
        stage("A", "2026-11-01T00:00:00Z"),  # over
        stage("B", "2026-11-20T00:00:00Z"),  # running
        stage("C", None),  # no end date: not over
        stage("D", None),
    ]

    assert [s.name for s in capture.capture_stages(stages, clock.now())] == ["B", "C"]


def test_a_stage_that_ends_exactly_now_is_over(clock):
    stages = [stage("A", "2026-11-12T12:30:15Z"), stage("B", None)]

    assert [s.name for s in capture.capture_stages(stages, clock.now())] == ["B"]


def test_with_every_stage_over_nothing_is_captured(clock):
    assert capture.capture_stages([stage("A", "2026-01-01T00:00:00Z")], clock.now()) == []


# --- queueing ---


def test_the_roster_is_queued_for_the_current_and_next_stage_only(db, cup, clock):
    count = capture.enqueue_for_tournament(db, cup, PRIORITY_AUTOMATIC, clock.now(), DAY)

    # Captain and player (no guest) on beatmaps 11, 12 and 13. 10 is over, 14 is too far ahead,
    # the unannounced tiebreaker has no beatmap, and 12 is in two pools but queued once.
    assert count == 6
    assert queued(db) == [
        (CAPTAIN, 11, 1),
        (CAPTAIN, 12, 1),
        (CAPTAIN, 13, 1),
        (PLAYER, 11, 1),
        (PLAYER, 12, 1),
        (PLAYER, 13, 1),
    ]


def test_pulls_made_within_the_refresh_window_are_skipped_and_older_ones_queued(db, cup, clock):
    recent = (clock.now() - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (clock.now() - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [
        capture_repo.QueueItem(PLAYER, 11, 0, 1, 0),
        capture_repo.QueueItem(PLAYER, 12, 0, 1, 0),
    ]
    capture_repo.mark_pulled(db, items[0], recent)
    capture_repo.mark_pulled(db, items[1], stale)

    capture.enqueue_for_tournament(db, cup, PRIORITY_AUTOMATIC, clock.now(), DAY)

    assert (PLAYER, 11, 1) not in queued(db)
    assert (PLAYER, 12, 1) in queued(db)
    assert (PLAYER, 13, 1) in queued(db)


def test_asking_in_person_ignores_the_refresh_window(db, cup, clock):
    now = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    capture_repo.mark_pulled(db, capture_repo.QueueItem(PLAYER, 11, 0, 1, 0), now)

    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), skip_within=None)

    assert (PLAYER, 11, 0) in queued(db)


def test_a_pull_is_only_skipped_for_its_own_game_mode(db, cup, clock):
    now = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    capture_repo.mark_pulled(db, capture_repo.QueueItem(PLAYER, 11, 1, 1, 0), now)  # taiko

    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), DAY)

    assert (PLAYER, 11, 1) in queued(db)


def test_queueing_a_waiting_pull_again_keeps_the_more_urgent_priority(db, cup, clock):
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), None)
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), None)

    assert {priority for _, _, priority in queued(db)} == {PRIORITY_REQUESTED}
    assert len(queued(db)) == 3  # still one row per pair


def test_a_team_is_queued_by_its_players(db, cup, clock):
    team = db.execute("INSERT INTO teams (tournament_id, name) VALUES (?, 'Rivals')", (cup.id,))
    for osu_id in (3001, 3002):
        db.execute(
            "INSERT INTO team_players (team_id, osu_id, username) VALUES (?, ?, ?)",
            (team.lastrowid, osu_id, f"rival{osu_id}"),
        )

    count = capture.enqueue_for_team(db, cup, team.lastrowid, PRIORITY_REQUESTED, clock.now())

    assert count == 6
    assert {osu_id for osu_id, _, _ in queued(db)} == {3001, 3002}


def test_the_hourly_run_queues_active_tournaments_only(db, cup, clock):
    archived = add_tournament(db, "Old Cup", status="archived", acronym="OC")
    add_member(db, archived, PLAYER, "player")
    old_stage = add_stage(db, archived)
    add_pool_map(db, old_stage, "NM1", beatmap_id=99)

    count = capture.enqueue_active(db, clock, refresh_hours=24)

    assert count == 6
    assert all(beatmap != 99 for _, beatmap, _ in queued(db))


# --- running the queue ---


@pytest.fixture
def osu_client(client):
    return client.app.state.osu


def test_a_pull_stores_the_players_passed_scores_as_lazer_scores(db, cup, clock, osu, osu_client):
    osu.add_api_score(
        11, PLAYER, 9001, total_score=800_000, accuracy=0.9712, statistics={"miss": 1}, mods=["HD"]
    )
    osu.add_api_score(11, PLAYER, 9002, total_score=700_000, passed=False)
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    result = run(capture.drain(db, osu_client, clock))

    assert result == capture.DrainResult(pulled=3, new_scores=1)
    assert stored(db) == [
        (
            11, PLAYER, "HD", 800_000, "lazer", 97.12, 512, 1, 1,
            "2026-10-01T12:34:56Z", "api", "9001", "https://osu.ppy.sh/scores/9001", None, None,
        )
    ]  # fmt: skip
    assert queued(db) == []


def test_pulling_again_adds_only_scores_not_seen_before(db, cup, clock, osu, osu_client):
    osu.add_api_score(11, PLAYER, 9001)
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)
    run(capture.drain(db, osu_client, clock))
    osu.add_api_score(11, PLAYER, 9002, total_score=900_000)
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    result = run(capture.drain(db, osu_client, clock))

    assert result.new_scores == 1
    assert [row[11] for row in stored(db)] == ["9001", "9002"]


def test_the_same_score_id_of_two_players_is_two_scores(db, cup, clock, osu, osu_client):
    osu.add_api_score(11, PLAYER, 9001)
    osu.add_api_score(11, CAPTAIN, 9001)
    capture.enqueue_players(db, cup, [PLAYER, CAPTAIN], PRIORITY_REQUESTED, clock.now(), None)

    run(capture.drain(db, osu_client, clock))

    assert len(stored(db)) == 2


def test_scores_of_any_age_are_stored(db, cup, clock, osu, osu_client):
    osu.add_api_score(11, PLAYER, 9001, ended_at="2019-03-04T05:06:07Z")
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    run(capture.drain(db, osu_client, clock))

    assert stored(db)[0][9] == "2019-03-04T05:06:07Z"


def test_a_map_osu_knows_no_scores_for_is_no_error_and_counts_as_pulled(
    db, cup, clock, osu, osu_client
):
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    result = run(capture.drain(db, osu_client, clock))

    assert result == capture.DrainResult(pulled=3, new_scores=0)
    assert queued(db) == []
    pulled = db.execute("SELECT COUNT(*) FROM capture_log WHERE osu_id = ?", (PLAYER,)).fetchone()
    assert pulled[0] == 3


def test_what_was_pulled_is_not_queued_again_by_an_automatic_run(db, cup, clock, osu, osu_client):
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), DAY)
    run(capture.drain(db, osu_client, clock))

    assert capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), DAY) == 0

    clock.current += DAY + timedelta(minutes=1)
    assert capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), DAY) == 3


def test_requested_pulls_run_before_automatic_ones_and_the_batch_is_limited(
    db, cup, clock, osu, osu_client
):
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), None)
    capture.enqueue_players(db, cup, [CAPTAIN], PRIORITY_REQUESTED, clock.now(), None)

    result = run(capture.drain(db, osu_client, clock, limit=3))

    assert result.pulled == 3
    assert queued(db) == [(PLAYER, 11, 1), (PLAYER, 12, 1), (PLAYER, 13, 1)]  # only the rest


def test_score_pulls_ask_osu_at_the_capture_priority(db, cup, clock, osu, osu_client):
    asked: list[Priority] = []

    class Recorder:
        async def acquire(self, priority: Priority) -> None:
            asked.append(priority)

    osu_client._limiter = Recorder()
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    run(capture.drain(db, osu_client, clock))

    assert asked[0] == Priority.INTERACTIVE  # the one-off token request
    assert asked[1:] == [Priority.CAPTURE] * 3


def test_pulls_never_run_faster_than_the_rate_limit_allows(db, cup, clock, osu, osu_client):
    osu_client._limiter = RateLimiter(rate_per_min=6000, burst=2)  # 100 a second, burst of 2
    capture.enqueue_players(db, cup, [PLAYER, CAPTAIN], PRIORITY_REQUESTED, clock.now(), None)
    started = time.monotonic()

    run(capture.drain(db, osu_client, clock))

    requests = len(osu.calls("GET", "/all")) + len(osu.calls("POST", "/oauth/token"))
    assert requests == 7  # six pulls and the token
    assert time.monotonic() - started >= (requests - 2) / 100 * 0.9  # all but the burst waited


# --- when osu! fails ---


def test_a_failing_pull_counts_an_attempt_keeps_the_rest_queued_and_raises(
    db, cup, clock, osu, osu_client
):
    osu.unavailable_paths.add("/api/v2/beatmaps/11/scores/users/2002/all")
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    with pytest.raises(OsuUnavailable):
        run(capture.drain(db, osu_client, clock))

    attempts = db.execute("SELECT beatmap_id, attempts FROM capture_queue ORDER BY beatmap_id")
    assert [tuple(row) for row in attempts] == [(11, 1), (12, 0), (13, 0)]
    assert stored(db) == []


def test_a_pull_that_keeps_failing_is_dropped_after_the_attempt_limit(
    db, cup, clock, osu, osu_client
):
    osu.unavailable_paths.add("/api/v2/beatmaps/11/scores/users/2002/all")
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(OsuUnavailable):
            run(capture.drain(db, osu_client, clock))

    assert (PLAYER, 11, 0) not in queued(db)
    assert not db.execute("SELECT 1 FROM capture_log WHERE beatmap_id = 11").fetchone()
    # The next automatic run queues it again, because it was never pulled.
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_AUTOMATIC, clock.now(), DAY)
    assert (PLAYER, 11, PRIORITY_AUTOMATIC) in queued(db)


def test_pulls_made_before_a_failure_are_kept(db, cup, clock, osu, osu_client):
    osu.add_api_score(11, PLAYER, 9001)
    osu.unavailable_paths.add("/api/v2/beatmaps/12/scores/users/2002/all")
    capture.enqueue_players(db, cup, [PLAYER], PRIORITY_REQUESTED, clock.now(), None)

    with pytest.raises(OsuUnavailable):
        run(capture.drain(db, osu_client, clock))

    assert len(stored(db)) == 1
    assert db.execute("SELECT beatmap_id FROM capture_log").fetchall()[0][0] == 11
