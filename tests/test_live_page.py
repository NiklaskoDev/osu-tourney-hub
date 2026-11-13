"""The live page, its event stream and the start and stop routes."""

import asyncio
import time
from datetime import timedelta

import pytest

from app.repos import match_events as events_repo
from app.repos.match_events import MatchEvent
from app.services.live import LiveHub
from app.services.live_state import FINISHED, RUNNING, STOPPED, LiveState
from app.web.routes import live as live_routes
from app.web.routes.live import END_EVENT, STATE_EVENT, sse_message, stream_frames
from tests.matchup_support import (
    CAPTAIN,
    GUEST,
    MATE,
    NM1,
    RIVAL_A,
    RIVAL_B,
    add_game,
    build_cup,
    fill_scores,
)
from tests.support import ADMIN_ID, add_member, add_tournament

STRANGER = 2009
LOBBY = 500


@pytest.fixture
def cup(db):
    cup = build_cup(db)
    fill_scores(db)
    return cup


@pytest.fixture
def match_id(db, cup, clock):
    start = (clock.current + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    match = add_game(db, cup, when=start)
    db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (LOBBY, match))
    return match


@pytest.fixture
def idle_follower(app, osu):
    """The app's follower with reads that never end their wait, so a started follow keeps
    running until the app shuts down. The lobby of the match is open on the fake osu!."""
    osu.add_match(LOBBY, end_time=None)

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    app.state.live._sleep = never
    return app.state.live


def url(cup, match, tail=""):
    return f"/t/{cup['id']}/live/{match}{tail}"


def state(status=RUNNING, wins=(0, 0), **changes) -> LiveState:
    return LiveState(
        1, status, None, "Alpha Squad vs Rival Crew", "Alpha Squad", "Rival Crew", *wins, **changes
    )


# --- the page ----------------------------------------------------------------------------------


def test_a_member_sees_the_state_and_the_stream_wiring(client, login, cup, match_id):
    login(MATE)

    page = client.get(url(cup, match_id))

    assert page.status_code == 200
    text = page.text
    assert "Alpha Squad vs Rival Crew" in text
    assert "Waiting for the lobby" in text and "Best of 3, first to 2" in text
    assert 'hx-ext="sse"' in text
    assert f'sse-connect="/t/{cup["id"]}/live/{match_id}/stream"' in text
    assert 'sse-swap="state"' in text and 'sse-close="end"' in text
    assert "No map has been played yet" in text


def test_the_page_shows_the_last_published_state(client, login, app, cup, match_id):
    app.state.live.hub.publish(
        match_id, state(wins=(1, 0), next_pick=None, prediction_notice="Nothing to suggest.")
    )
    login(MATE)

    text = client.get(url(cup, match_id)).text

    assert 'class="live__wins num">1<span class="muted"> - </span>0' in text
    assert "Nothing to suggest." in text


def test_the_page_lists_what_the_lobby_stored(client, login, db, cup, match_id):
    maps = cup["maps"]
    events_repo.replace_source(
        db,
        match_id,
        "mp",
        [MatchEvent(100, "pick", None, maps["NM1"], NM1, 1, 1_800_000, 1_600_000, "mp")],
    )
    login(MATE)

    text = client.get(url(cup, match_id)).text

    assert "Maps played" in text and "1,800,000" in text and "1,600,000" in text
    assert "Next pick" in text


def test_a_non_member_and_an_unknown_or_foreign_match_are_not_found(
    client, login, db, cup, match_id
):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, STRANGER, "player")

    login(STRANGER)
    assert client.get(url(cup, match_id)).status_code == 404
    assert client.get(url(cup, match_id, "/stream")).status_code == 404

    login(CAPTAIN)
    for tail in (str(match_id + 100), "abc", "-1"):
        assert client.get(f"/t/{cup['id']}/live/{tail}").status_code == 404, tail
    assert client.get(f"/t/{other}/live/{match_id}").status_code == 404


def test_the_page_needs_a_login(client, cup, match_id):
    response = client.get(url(cup, match_id), follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert "/live/" not in response.headers["location"]


def test_the_match_page_links_to_the_live_view(client, login, cup, match_id):
    login(MATE)

    assert f'href="{url(cup, match_id)}"' in client.get(f"/t/{cup['id']}/matches/{match_id}").text


@pytest.mark.parametrize(
    ("who", "sees_button"), [(CAPTAIN, True), (ADMIN_ID, True), (MATE, False), (GUEST, False)]
)
def test_only_captains_get_the_start_button(client, login, cup, match_id, who, sees_button):
    login(who)

    text = client.get(url(cup, match_id)).text

    assert (f'action="{url(cup, match_id, "/start")}"' in text) is sees_button
    assert "/stop" not in text


def test_without_a_lobby_the_start_button_is_disabled_and_says_why(
    client, login, db, cup, match_id
):
    db.execute("UPDATE matches SET mp_match_id = NULL WHERE id = ?", (match_id,))
    login(CAPTAIN)

    text = client.get(url(cup, match_id)).text

    assert "No multiplayer lobby yet" in text
    assert "disabled" in text.split("Start live updates")[0].rsplit("<button", 1)[1]


def test_an_archived_tournament_shows_no_controls(client, login, db, cup, match_id):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    text = client.get(url(cup, match_id)).text

    assert "This tournament is archived" in text and "/start" not in text


# --- the stream --------------------------------------------------------------------------------


def test_the_stream_sends_the_state_as_server_sent_events_and_ends_when_the_lobby_is_closed(
    client, login, db, cup, match_id
):
    db.execute("UPDATE matches SET status = 'done' WHERE id = ?", (match_id,))
    login(MATE)

    response = client.get(url(cup, match_id, "/stream"))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    body = response.text
    assert body.startswith(f"event: {STATE_EVENT}\ndata: ")
    assert "The lobby is closed" in body and "Alpha Squad" in body
    assert body.endswith(f"event: {END_EVENT}\ndata: closed\n\n")
    # The data field repeats on every line, as the format needs.
    assert all(line.startswith(("event: ", "data: ")) for line in body.split("\n") if line)


def test_the_stream_of_a_hidden_tournament_stays_hidden_from_outsiders(
    client, login, db, cup, match_id
):
    db.execute("UPDATE tournaments SET status = 'hidden' WHERE id = ?", (cup["id"],))
    login(MATE)

    assert client.get(url(cup, match_id, "/stream")).status_code == 404


def test_one_event_carries_every_line_of_its_data():
    assert sse_message("state", "one\ntwo") == "event: state\ndata: one\ndata: two\n\n"
    assert sse_message("end", "") == "event: end\ndata: \n\n"


async def collect(frames, count: int) -> list[str]:
    seen = []
    async for frame in frames:
        seen.append(frame)
        if len(seen) == count:
            break
    return seen


@pytest.mark.anyio
async def test_the_stream_sends_the_current_state_then_every_published_one():
    hub = LiveHub()
    frames = stream_frames(
        hub, 1, state(wins=(0, 0)), 60, render_fn=lambda s: f"wins {s.left_wins}"
    )

    first = await anext(frames)
    assert first == "event: state\ndata: wins 0\n\n"
    assert hub.subscriber_count(1) == 1
    hub.publish(1, state(wins=(1, 0)))
    assert await anext(frames) == "event: state\ndata: wins 1\n\n"
    hub.publish(1, state(FINISHED, wins=(2, 0)))
    assert await anext(frames) == "event: state\ndata: wins 2\n\n"
    assert await anext(frames) == "event: end\ndata: closed\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(frames)
    assert hub.subscriber_count(1) == 0


@pytest.mark.anyio
async def test_a_quiet_stream_sends_a_heartbeat_comment():
    hub = LiveHub()
    frames = stream_frames(hub, 1, state(), 0.01, render_fn=lambda s: "x")

    seen = await collect(frames, 3)

    assert seen == ["event: state\ndata: x\n\n", ": heartbeat\n\n", ": heartbeat\n\n"]
    await frames.aclose()


@pytest.mark.anyio
async def test_a_page_that_disconnects_is_dropped_from_the_hub():
    hub = LiveHub()
    frames = stream_frames(hub, 1, state(), 60, render_fn=lambda s: "x")
    await anext(frames)
    assert hub.subscriber_count(1) == 1

    await frames.aclose()  # what the server does when the browser goes away

    assert hub.subscriber_count(1) == 0


@pytest.mark.anyio
async def test_a_stopped_follow_keeps_the_stream_open_for_a_restart():
    hub = LiveHub()
    frames = stream_frames(hub, 1, state(STOPPED), 0.01, render_fn=lambda s: "x")

    assert await collect(frames, 2) == ["event: state\ndata: x\n\n", ": heartbeat\n\n"]
    await frames.aclose()


def test_the_heartbeat_comes_every_fifteen_seconds():
    assert live_routes.HEARTBEAT_SECONDS == 15


# --- start and stop ----------------------------------------------------------------------------


def start(client, csrf, cup, match, follow=True):
    return client.post(url(cup, match, "/start"), data=csrf(), follow_redirects=follow)


def test_a_captain_starts_and_stops_live_updates(client, login, csrf, cup, match_id, idle_follower):
    login(CAPTAIN)

    started = start(client, csrf, cup, match_id)

    assert started.status_code == 200
    assert "Live updates started." in started.text
    assert "Stop live updates" in started.text and "Start live updates" not in started.text
    assert idle_follower.is_following(match_id)

    stopped = client.post(url(cup, match_id, "/stop"), data=csrf(), follow_redirects=True)

    assert "Live updates stopped." in stopped.text
    assert not idle_follower.is_following(match_id)
    assert "Start live updates" in stopped.text
    assert idle_follower.hub.latest(match_id).status == STOPPED


def test_starting_twice_and_stopping_when_nothing_runs_are_harmless(
    client, login, csrf, cup, match_id, idle_follower
):
    login(CAPTAIN)

    assert (
        "Live updates were not running."
        in client.post(url(cup, match_id, "/stop"), data=csrf(), follow_redirects=True).text
    )
    start(client, csrf, cup, match_id)
    again = start(client, csrf, cup, match_id)

    assert "Live updates are already running." in again.text
    assert idle_follower.is_following(match_id)


@pytest.mark.parametrize(
    ("who", "status"), [(MATE, 403), (GUEST, 403), (STRANGER, 404), (ADMIN_ID, 303)]
)
def test_who_may_start_and_stop(client, login, csrf, cup, match_id, idle_follower, who, status):
    login(who)

    started = start(client, csrf, cup, match_id, follow=False)
    stopped = client.post(url(cup, match_id, "/stop"), data=csrf(), follow_redirects=False)

    assert started.status_code == status and stopped.status_code == status
    assert not idle_follower.is_following(match_id)  # an admin's start was stopped again


def test_a_stranger_cannot_reach_the_follower_at_all(
    client, login, csrf, db, cup, match_id, idle_follower
):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, STRANGER, "captain")
    login(STRANGER)

    response = client.post(f"/t/{other}/live/{match_id}/start", data=csrf())

    assert response.status_code == 404
    assert not idle_follower.is_following(match_id)


def test_starting_needs_a_csrf_token(client, login, cup, match_id, idle_follower):
    login(CAPTAIN)

    assert client.post(url(cup, match_id, "/start"), data={}).status_code == 403
    assert client.post(url(cup, match_id, "/stop"), data={}).status_code == 403
    assert not idle_follower.is_following(match_id)


def test_a_match_without_a_lobby_cannot_be_started(
    client, login, csrf, db, cup, match_id, idle_follower
):
    db.execute("UPDATE matches SET mp_match_id = NULL WHERE id = ?", (match_id,))
    login(CAPTAIN)

    response = start(client, csrf, cup, match_id, follow=False)

    assert response.status_code == 409 and "no multiplayer lobby" in response.text
    assert not idle_follower.is_following(match_id)


def test_nothing_starts_or_stops_in_an_archived_tournament(
    client, login, csrf, db, cup, match_id, idle_follower
):
    login(CAPTAIN)
    token = csrf()
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))

    started = client.post(url(cup, match_id, "/start"), data=token)
    stopped = client.post(url(cup, match_id, "/stop"), data=token)

    assert started.status_code == 409 and stopped.status_code == 409
    assert "archived" in started.text
    assert not idle_follower.is_following(match_id)


def test_a_started_follow_reads_the_lobby_and_the_page_shows_the_score(
    client, login, csrf, osu, cup, match_id, idle_follower
):
    osu.add_game(
        LOBBY, NM1, [(CAPTAIN, 900_000), (MATE, 900_000), (RIVAL_A, 800_000), (RIVAL_B, 800_000)]
    )
    login(CAPTAIN)

    page = start(client, csrf, cup, match_id)

    assert page.status_code == 200
    # The follow task reads the lobby at once; a reload shows what it found.
    for _ in range(100):
        text = client.get(url(cup, match_id)).text
        if 'num">1<span class="muted"> - </span>0' in text:
            break
        time.sleep(0.05)
    assert 'num">1<span class="muted"> - </span>0' in text
    assert "Following the lobby" in text
