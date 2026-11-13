"""Live match mode: the hub, the follow loop against a scripted fake lobby, the state it builds and
the automatic start."""

import asyncio
from datetime import timedelta
from unittest import mock

import pytest

from app.clients.ratelimit import Priority
from app.repos import match_events as events_repo
from app.repos import matches as matches_repo
from app.repos import tournaments as tournaments_repo
from app.repos.match_events import MatchEvent
from app.services.live import (
    MAX_FAILED_POLLS,
    NOTICE_LOBBY_GONE,
    NOTICE_NO_LOBBY,
    NOTICE_RETRYING,
    NOTICE_TIMEOUT,
    NOTICE_UNREACHABLE,
    LiveFollower,
    LiveHub,
)
from app.services.live_state import (
    FINISHED,
    NOTICE_PLAYING,
    RUNNING,
    STOPPED,
    WAITING,
    LiveState,
    build_state,
)
from tests.matchup_support import (
    CAPTAIN,
    HD1,
    MATE,
    NM1,
    NM2,
    RIVAL_A,
    RIVAL_B,
    build_cup,
    fill_scores,
)
from tests.matchup_support import add_game as add_cup_match

pytestmark = pytest.mark.anyio

LOBBY = 500
WE_WIN = [(CAPTAIN, 900_000), (MATE, 900_000), (RIVAL_A, 800_000), (RIVAL_B, 800_000)]
THEY_WIN = [(CAPTAIN, 700_000), (MATE, 700_000), (RIVAL_A, 900_000), (RIVAL_B, 900_000)]
CLOSED = "2026-11-12T13:00:00+00:00"


def state_of(wins: tuple[int, int], status: str = RUNNING) -> LiveState:
    return LiveState(1, status, None, "A vs B", "A", "B", wins[0], wins[1])


@pytest.fixture
def cup(db):
    cup = build_cup(db)
    fill_scores(db)
    return cup


@pytest.fixture
def match_id(db, cup, clock):
    """Our match 10 minutes from now, with a lobby that is still open."""
    start = (clock.current + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    match = add_cup_match(db, cup, when=start)
    db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (LOBBY, match))
    return match


@pytest.fixture
def lobby(osu):
    return osu.add_match(LOBBY, end_time=None)


def close(lobby) -> None:
    lobby["match"]["end_time"] = CLOSED


class Pacer:
    """The follower's sleep: moves the clock on and runs the next scripted change of the lobby,
    so a test decides what osu! shows at each read. Nothing really waits."""

    def __init__(self, clock, steps=(), advance: timedelta | None = None):
        self.clock = clock
        self.steps = list(steps)
        self.advance = advance
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.current += self.advance or timedelta(seconds=seconds)
        if self.steps:
            self.steps.pop(0)()
        await asyncio.sleep(0)


@pytest.fixture
def make(settings, clock, scan_client):
    def build(*steps, advance=None) -> tuple[LiveFollower, Pacer]:
        pacer = Pacer(clock, steps, advance)
        return LiveFollower(settings, clock, scan_client, sleep=pacer), pacer

    return build


async def never(seconds: float) -> None:
    """A sleep that does not end, to keep a follow running until the test stops it."""
    await asyncio.Event().wait()


async def settle(condition) -> None:
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition did not come true")


async def finish(follower: LiveFollower, match_id: int) -> None:
    await settle(lambda: not follower.is_following(match_id))


def drain(queue: asyncio.Queue) -> list[LiveState]:
    states = []
    while not queue.empty():
        states.append(queue.get_nowait())
    return states


def shown(states: list[LiveState]) -> list[tuple]:
    return [(s.status, s.left_wins, s.right_wins) for s in states]


# --- the hub -----------------------------------------------------------------------------------


async def test_the_hub_gives_every_subscriber_of_the_match_the_state():
    hub = LiveHub()
    first, second, other = hub.subscribe(1), hub.subscribe(1), hub.subscribe(2)

    delivered = hub.publish(1, state_of((1, 0)))

    assert delivered == 2
    assert first.get_nowait() == second.get_nowait() == state_of((1, 0))
    assert other.empty()


async def test_the_hub_drops_a_subscriber_that_went_away():
    hub = LiveHub()
    staying, leaving = hub.subscribe(1), hub.subscribe(1)

    hub.unsubscribe(1, leaving)
    hub.unsubscribe(1, leaving)  # leaving twice is harmless

    assert hub.subscriber_count(1) == 1
    assert hub.publish(1, state_of((1, 0))) == 1
    assert leaving.empty() and staying.qsize() == 1
    hub.unsubscribe(1, staying)
    assert hub.subscriber_count(1) == 0
    assert hub.publish(1, state_of((2, 0))) == 0


async def test_a_slow_subscriber_loses_its_oldest_states_and_keeps_the_newest():
    hub = LiveHub(queue_size=2)
    slow = hub.subscribe(1)

    for wins in range(1, 5):
        hub.publish(1, state_of((wins, 0)))

    assert [s.left_wins for s in drain(slow)] == [3, 4]


async def test_the_hub_remembers_the_latest_state_for_a_page_opened_later():
    hub = LiveHub()
    assert hub.latest(1) is None

    hub.publish(1, state_of((1, 0)))
    hub.publish(1, state_of((1, 1)))

    assert hub.latest(1) == state_of((1, 1))


# --- following a lobby -------------------------------------------------------------------------


async def test_the_running_score_follows_the_lobby_until_it_closes(
    db, osu, lobby, match_id, make, clock
):
    osu.add_game(LOBBY, NM1, WE_WIN)
    second = osu.add_game(LOBBY, NM2, ended=False)
    third: dict = {}
    follower, pacer = make(
        lambda: (
            osu.finish_game(LOBBY, second, THEY_WIN),
            third.update(osu.add_game(LOBBY, HD1, ended=False)),
        ),
        lambda: (osu.finish_game(LOBBY, third, WE_WIN), close(lobby)),
    )
    queue = follower.hub.subscribe(match_id)

    assert follower.start(match_id) is True
    await finish(follower, match_id)

    states = drain(queue)
    assert shown(states) == [(RUNNING, 1, 0), (RUNNING, 1, 1), (FINISHED, 2, 1)]
    assert [s.current.slot if s.current else None for s in states] == ["NM2", "HD1", None]
    assert states[-1].decided and states[-1].is_final
    assert pacer.slept == [10, 10]  # LIVE_POLL_SECONDS between two reads
    assert follower.hub.latest(match_id) == states[-1]
    stored = events_repo.list_for_match(db, match_id)
    assert [(e.kind, e.source, e.winner_side) for e in stored] == [
        ("pick", "mp", w) for w in (1, 2, 1)
    ]


async def test_the_first_read_takes_the_whole_lobby_and_later_ones_only_what_is_new(
    osu, scan_client, lobby, match_id, make
):
    osu.add_game(LOBBY, NM1, WE_WIN)
    follower, _ = make(lambda: close(lobby))

    with (
        mock.patch.object(scan_client, "fetch_match", wraps=scan_client.fetch_match) as whole,
        mock.patch.object(
            scan_client, "fetch_match_after", wraps=scan_client.fetch_match_after
        ) as newer,
    ):
        follower.start(match_id)
        await finish(follower, match_id)

    assert whole.call_count == 1 and newer.call_count == 1
    assert whole.call_args.kwargs["priority"] == Priority.LIVE
    assert newer.call_args.kwargs["priority"] == Priority.LIVE
    requests = osu.match_requests(LOBBY)
    assert "after" not in requests[0].url.params
    assert requests[1].url.params["after"] == str(lobby["events"][0]["id"])


async def test_a_game_in_progress_is_read_again_when_it_ends(osu, lobby, match_id, make):
    running = osu.add_game(LOBBY, NM1, ended=False)
    follower, _ = make(lambda: (osu.finish_game(LOBBY, running, WE_WIN), close(lobby)))
    queue = follower.hub.subscribe(match_id)

    follower.start(match_id)
    await finish(follower, match_id)

    states = drain(queue)
    assert shown(states) == [(RUNNING, 0, 0), (FINISHED, 1, 0)]
    assert states[0].current.slot == "NM1" and states[0].prediction_notice == NOTICE_PLAYING
    assert states[1].played[0].winner == "Alpha Squad"


async def test_nothing_is_published_while_nothing_changes(osu, lobby, match_id, make):
    osu.add_game(LOBBY, NM1, WE_WIN)
    follower, _ = make(lambda: None, lambda: None, lambda: close(lobby))
    queue = follower.hub.subscribe(match_id)

    follower.start(match_id)
    await finish(follower, match_id)

    assert shown(drain(queue)) == [(RUNNING, 1, 0), (FINISHED, 1, 0)]


async def test_a_lobby_that_is_already_closed_is_read_once_and_finishes(osu, lobby, match_id, make):
    osu.add_game(LOBBY, NM1, WE_WIN)
    close(lobby)
    follower, pacer = make()

    follower.start(match_id)
    await finish(follower, match_id)

    assert follower.hub.latest(match_id).status == FINISHED
    assert pacer.slept == []
    assert len(osu.match_requests(LOBBY)) == 1


async def test_following_stops_after_four_hours(osu, lobby, match_id, make):
    osu.add_game(LOBBY, NM1, WE_WIN)
    follower, pacer = make(advance=timedelta(hours=1))

    follower.start(match_id)
    await finish(follower, match_id)

    last = follower.hub.latest(match_id)
    assert (last.status, last.notice) == (STOPPED, NOTICE_TIMEOUT)
    assert len(pacer.slept) == 4 and len(osu.match_requests(LOBBY)) == 5
    assert not last.is_final  # a captain can start it again


async def test_a_lobby_osu_does_not_have_stops_the_follow(osu, lobby, match_id, make):
    osu.match_statuses[LOBBY] = 404
    follower, _ = make()

    follower.start(match_id)
    await finish(follower, match_id)

    last = follower.hub.latest(match_id)
    assert (last.status, last.notice) == (STOPPED, NOTICE_LOBBY_GONE)


async def test_osu_not_answering_is_shown_and_clears_when_it_answers_again(
    osu, lobby, match_id, make
):
    osu.add_game(LOBBY, NM1, WE_WIN)
    osu.match_statuses[LOBBY] = 503
    follower, _ = make(lambda: osu.match_statuses.pop(LOBBY), lambda: close(lobby))
    queue = follower.hub.subscribe(match_id)

    follower.start(match_id)
    await finish(follower, match_id)

    states = drain(queue)
    assert [(s.status, s.notice) for s in states] == [
        (RUNNING, NOTICE_RETRYING),
        (RUNNING, None),
        (FINISHED, None),
    ]
    assert states[-1].left_wins == 1


async def test_following_gives_up_after_osu_failed_several_reads_in_a_row(
    osu, lobby, match_id, make
):
    osu.match_statuses[LOBBY] = 503
    follower, pacer = make()

    follower.start(match_id)
    await finish(follower, match_id)

    last = follower.hub.latest(match_id)
    assert (last.status, last.notice) == (STOPPED, NOTICE_UNREACHABLE)
    assert len(pacer.slept) == MAX_FAILED_POLLS - 1


async def test_a_match_without_a_lobby_cannot_be_followed(db, match_id, make):
    db.execute("UPDATE matches SET mp_match_id = NULL WHERE id = ?", (match_id,))
    follower, _ = make()

    follower.start(match_id)
    await finish(follower, match_id)

    last = follower.hub.latest(match_id)
    assert (last.status, last.notice) == (STOPPED, NOTICE_NO_LOBBY)


async def test_a_captain_stop_ends_the_follow_and_tells_the_pages(
    osu, lobby, match_id, settings, clock, scan_client
):
    osu.add_game(LOBBY, NM1, WE_WIN)
    follower = LiveFollower(settings, clock, scan_client, sleep=never)
    queue = follower.hub.subscribe(match_id)
    follower.start(match_id)
    await settle(lambda: not queue.empty())

    assert follower.is_following(match_id)
    assert await follower.stop(match_id) is True

    assert not follower.is_following(match_id)
    assert [s.status for s in drain(queue)] == [RUNNING, STOPPED]
    assert await follower.stop(match_id) is False


async def test_stopping_before_the_first_read_still_ends_cleanly(
    match_id, lobby, settings, clock, scan_client
):
    follower = LiveFollower(settings, clock, scan_client, sleep=never)

    follower.start(match_id)
    await follower.stop(match_id)

    assert not follower.is_following(match_id)
    assert follower.hub.latest(match_id).status == STOPPED
    assert await follower.start_due() == 0  # a stopped match is not started again by itself


async def test_starting_a_followed_match_again_changes_nothing(
    match_id, lobby, settings, clock, scan_client
):
    follower = LiveFollower(settings, clock, scan_client, sleep=never)

    assert follower.start(match_id) is True
    assert follower.start(match_id) is False
    await follower.stop_all()

    assert not follower.is_following(match_id)


async def test_a_stopped_match_can_be_started_again_by_a_captain(
    osu, lobby, match_id, settings, clock, scan_client
):
    follower = LiveFollower(settings, clock, scan_client, sleep=never)
    follower.start(match_id)
    await follower.stop(match_id)

    assert follower.start(match_id) is True
    await follower.stop_all()


# --- the automatic start -----------------------------------------------------------------------


@pytest.fixture
def idle(settings, clock, scan_client):
    """A follower whose reads never end their wait, so a started follow stays running."""
    return LiveFollower(settings, clock, scan_client, sleep=never)


def schedule(db, match_id, clock, delta: timedelta) -> None:
    when = (clock.current + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE matches SET scheduled_at = ? WHERE id = ?", (when, match_id))


@pytest.mark.parametrize(
    ("delta", "starts"),
    [
        (timedelta(minutes=29), True),
        (timedelta(minutes=-30), True),  # running already, e.g. after a restart
        (timedelta(hours=-3), True),
        (timedelta(minutes=31), False),
        (timedelta(hours=-5), False),
    ],
)
async def test_a_match_is_followed_from_half_an_hour_before_it_starts_until_four_hours_after(
    db, osu, lobby, match_id, clock, idle, delta, starts
):
    schedule(db, match_id, clock, delta)

    assert await idle.start_due() == (1 if starts else 0)
    assert idle.is_following(match_id) is starts
    await idle.stop_all()


async def test_the_automatic_start_leaves_other_matches_alone(
    db, cup, osu, lobby, match_id, clock, idle
):
    near = (clock.current + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    others = {
        "not ours": db.execute(
            "INSERT INTO matches (tournament_id, stage_id, team1_id, team2_id, scheduled_at, "
            "mp_match_id, status, updated_at) VALUES (?, ?, ?, ?, ?, 600, 'scheduled', 'x')",
            (cup["id"], cup["stage"], cup["rivals"], None, near),
        ).lastrowid,
        "no lobby": add_cup_match(db, cup, when=near),
        "done": add_cup_match(db, cup, when=near, status="done"),
    }
    db.execute("UPDATE matches SET mp_match_id = 601 WHERE id = ?", (others["done"],))

    assert await idle.start_due() == 1
    assert [name for name, mid in others.items() if idle.is_following(mid)] == []
    assert idle.is_following(match_id)
    await idle.stop_all()


async def test_an_archived_tournament_is_not_followed_by_itself(
    db, cup, osu, lobby, match_id, idle
):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))

    assert await idle.start_due() == 0


async def test_a_match_followed_to_the_end_is_not_started_again(osu, lobby, match_id, make):
    osu.add_game(LOBBY, NM1, WE_WIN)
    close(lobby)
    follower, _ = make()
    assert await follower.start_due() == 1
    await finish(follower, match_id)

    assert await follower.start_due() == 0
    assert len(osu.match_requests(LOBBY)) == 1


async def test_a_match_that_is_already_followed_is_not_started_twice(osu, lobby, match_id, idle):
    assert await idle.start_due() == 1
    assert await idle.start_due() == 0
    await idle.stop_all()


# --- what a page shows -------------------------------------------------------------------------


async def test_a_page_opened_before_the_match_reads_the_state_from_the_stored_events(
    db, cup, match_id, idle
):
    match = matches_repo.get(db, match_id)
    tournament = tournaments_repo.get(db, cup["id"])

    state = idle.current_state(db, tournament, match)

    assert (state.status, state.left_wins, state.right_wins) == (WAITING, 0, 0)
    assert state.title == "Alpha Squad vs Rival Crew"
    assert state.best_of == 3 and state.first_to == 2


async def test_a_finished_match_shows_as_closed_without_a_follower(db, cup, match_id, idle):
    db.execute("UPDATE matches SET status = 'done' WHERE id = ?", (match_id,))
    match = matches_repo.get(db, match_id)

    state = idle.current_state(db, tournaments_repo.get(db, cup["id"]), match)

    assert state.status == FINISHED and state.is_final


def pick(seq, pool_map, beatmap, winner, team1_score, team2_score, side=None, source="mp"):
    return MatchEvent(
        seq, "pick", side, pool_map, beatmap, winner, team1_score, team2_score, source
    )


def build(db, cup, match_id, status=RUNNING, **kwargs):
    match = matches_repo.get(db, match_id)
    return build_state(db, tournaments_repo.get(db, cup["id"]), match, status, **kwargs)


async def test_the_next_pick_is_the_models_choice_for_the_left_team_when_the_picker_is_unknown(
    db, cup, match_id
):
    maps = cup["maps"]
    events_repo.replace_source(
        db, match_id, "mp", [pick(100, maps["NM1"], NM1, 1, 1_800_000, 1_600_000)]
    )

    state = build(db, cup, match_id)

    next_pick = state.next_pick
    assert (state.left_wins, state.right_wins) == (1, 0)
    assert next_pick.team == "Alpha Squad" and next_pick.picker_known is False
    assert next_pick.map.slot in {"NM2", "HD1"} and not next_pick.decider
    assert next_pick.team_chance > 0.5
    assert state.left_win_prob > 0.5
    assert [m.slot for m in state.open_maps] == ["NM2", "HD1", "TB"]


async def test_the_tiebreaker_is_next_when_the_series_is_one_win_each_from_the_end(
    db, cup, match_id
):
    maps = cup["maps"]
    events_repo.replace_source(
        db,
        match_id,
        "mp",
        [
            pick(100, maps["NM1"], NM1, 1, 1_800_000, 1_600_000),
            pick(101, maps["NM2"], NM2, 2, 1_400_000, 1_600_000),
        ],
    )

    state = build(db, cup, match_id)

    assert state.next_pick.decider and state.next_pick.team is None
    assert state.next_pick.map.slot == "TB"


async def test_a_won_series_has_no_next_pick(db, cup, match_id):
    maps = cup["maps"]
    events_repo.replace_source(
        db,
        match_id,
        "mp",
        [
            pick(100, maps["NM1"], NM1, 1, 1_800_000, 1_600_000),
            pick(101, maps["NM2"], NM2, 1, 1_800_000, 1_600_000),
        ],
    )

    state = build(db, cup, match_id)

    assert state.decided and state.next_pick is None and state.prediction_notice is None


async def test_a_banned_map_is_listed_and_never_suggested(db, cup, match_id):
    maps = cup["maps"]
    ban = MatchEvent(1, "ban", 2, maps["NM2"], NM2, None, None, None, "wybin")
    events_repo.replace_source(db, match_id, "wybin", [ban])
    events_repo.replace_source(
        db, match_id, "mp", [pick(100, maps["NM1"], NM1, 1, 1_800_000, 1_600_000)]
    )

    state = build(db, cup, match_id)

    assert [(r.kind, r.map.slot, r.team) for r in state.removed] == [("ban", "NM2", "Rival Crew")]
    assert state.next_pick.map.slot == "HD1"
    assert "NM2" not in [m.slot for m in state.open_maps]


async def test_the_next_picker_alternates_from_a_pick_a_captain_named(db, cup, match_id):
    maps = cup["maps"]
    events_repo.save_manual(
        db, match_id, pick(100, maps["NM1"], NM1, 1, 1_800_000, 1_600_000, side=1, source="manual")
    )

    state = build(db, cup, match_id)

    assert state.next_pick.team == "Rival Crew" and state.next_pick.picker_known is True


async def test_the_left_team_is_ours_even_when_wybin_lists_us_second(db, cup, clock):
    match = add_cup_match(db, cup, ours_first=False)
    maps = cup["maps"]
    # Side 2 (ours here) won with 1 800 000 against 1 600 000.
    events_repo.replace_source(
        db, match, "mp", [pick(100, maps["NM1"], NM1, 2, 1_600_000, 1_800_000)]
    )

    state = build(db, cup, match)

    assert (state.left_name, state.right_name) == ("Alpha Squad", "Rival Crew")
    assert (state.left_wins, state.right_wins) == (1, 0)
    game = state.played[0]
    assert (game.winner, game.left_score, game.right_score) == ("Alpha Squad", 1_800_000, 1_600_000)


async def test_a_match_without_teams_shows_why_there_is_no_suggestion(db, cup):
    match = add_cup_match(db, cup)
    db.execute("UPDATE matches SET team2_id = NULL WHERE id = ?", (match,))

    state = build(db, cup, match)

    assert state.next_pick is None and "not decided yet" in state.prediction_notice
    assert state.right_name == "To be decided"
