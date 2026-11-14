"""The home page's "Upcoming matches": our matches across the person's active tournaments."""

import re
from datetime import UTC, datetime

import pytest

from app.services import schedule
from tests.support import (
    ADMIN_ID,
    add_match,
    add_member,
    add_stage,
    add_team,
    add_tournament,
)

CAPTAIN, PLAYER, OUTSIDER = 2001, 2002, 2009
NOW = datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC)  # the test clock


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    stage = add_stage(db, tid, "Round of 16")
    ours = add_team(db, tid, "Alpha Squad", [(CAPTAIN, "c")], ours=True)
    rival = add_team(db, tid, "Rival Crew")
    third = add_team(db, tid, "Third Wheel")
    return {"id": tid, "stage": stage, "ours": ours, "rival": rival, "third": third}


def view_titles(items):
    return [item.view.opponent_name for item in items]


# --- upcoming_for_user --------------------------------------------------------------------------


def test_it_lists_our_matches_soonest_first_with_their_tournament(db, cup):
    add_match(db, cup["id"], cup["ours"], cup["third"], "2026-11-20T19:00:00Z", cup["stage"])
    add_match(db, cup["id"], cup["rival"], cup["ours"], "2026-11-14T19:00:00Z", cup["stage"])

    items = schedule.upcoming_for_user(db, CAPTAIN, NOW)

    assert view_titles(items) == ["Rival Crew", "Third Wheel"]
    assert items[0].tournament.id == cup["id"]
    assert items[0].view.stage_name == "Round of 16"


def test_matches_between_other_teams_are_left_out(db, cup):
    add_match(db, cup["id"], cup["rival"], cup["third"], "2026-11-14T19:00:00Z")

    assert schedule.upcoming_for_user(db, CAPTAIN, NOW) == []


def test_only_tournaments_the_person_belongs_to_count(db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, OUTSIDER, "captain")
    mine = add_team(db, other, "Other Alpha", [(OUTSIDER, "o")], ours=True)
    add_match(db, other, mine, add_team(db, other, "Other Rival"), "2026-11-14T19:00:00Z")
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-15T19:00:00Z")

    assert view_titles(schedule.upcoming_for_user(db, CAPTAIN, NOW)) == ["Rival Crew"]
    assert view_titles(schedule.upcoming_for_user(db, OUTSIDER, NOW)) == ["Other Rival"]


def test_archived_tournaments_are_left_out(db, cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-14T19:00:00Z")

    assert schedule.upcoming_for_user(db, CAPTAIN, NOW) == []


def test_done_matches_and_matches_that_started_over_90_minutes_ago_are_left_out(db, cup):
    add_match(
        db, cup["id"], cup["ours"], cup["rival"], "2026-11-12T13:00:00Z", status="done"
    )  # later today, but finished
    add_match(db, cup["id"], cup["ours"], cup["third"], "2026-11-12T10:59:00Z")  # 91 minutes ago

    assert schedule.upcoming_for_user(db, CAPTAIN, NOW) == []


def test_a_match_that_started_30_minutes_ago_still_shows(db, cup):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-12T12:00:00Z")

    assert view_titles(schedule.upcoming_for_user(db, CAPTAIN, NOW)) == ["Rival Crew"]


def test_a_match_without_a_time_is_left_out(db, cup):
    add_match(db, cup["id"], cup["ours"], cup["rival"], None)

    assert schedule.upcoming_for_user(db, CAPTAIN, NOW) == []


def test_the_limit_applies_after_the_order(db, cup):
    for day in (20, 15, 18, 16):
        add_match(db, cup["id"], cup["ours"], cup["rival"], f"2026-11-{day}T19:00:00Z")

    items = schedule.upcoming_for_user(db, CAPTAIN, NOW, limit=2)

    assert [item.view.match.scheduled_at[:10] for item in items] == ["2026-11-15", "2026-11-16"]


def test_the_calendar_feed_still_lists_its_events(db, cup):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-14T19:00:00Z", label="Match 4")

    events = schedule.feed_events(db, CAPTAIN)

    assert [event.summary for event in events] == ["[SC] Match 4: Alpha Squad vs Rival Crew"]


# --- the page -----------------------------------------------------------------------------------


def test_the_home_page_marks_the_first_match_as_next(client, login, db, cup):
    add_match(db, cup["id"], cup["ours"], cup["third"], "2026-11-20T19:00:00Z", cup["stage"])
    add_match(db, cup["id"], cup["rival"], cup["ours"], "2026-11-14T19:00:00Z", cup["stage"])
    login(CAPTAIN)

    html = client.get("/").text

    assert "Upcoming matches" in html
    assert html.count("upcoming__item--next") == 1
    assert html.index("vs Rival Crew") < html.index("vs Third Wheel")
    assert f'href="/t/{cup["id"]}/matches/' in html
    assert "SC" in html


def test_the_home_page_says_so_when_there_is_no_upcoming_match(client, login, cup):
    login(CAPTAIN)

    html = client.get("/").text

    assert "No upcoming matches in your tournaments." in html
    assert "upcoming__item" not in html


def test_an_admin_on_no_roster_gets_no_upcoming_section(client, login, cup):
    login(ADMIN_ID)

    html = client.get("/").text

    assert "Upcoming matches" not in html


def test_a_player_without_any_tournament_gets_no_upcoming_section(client, login):
    login(OUTSIDER)

    html = client.get("/").text

    assert "Upcoming matches" not in html
    assert "not on a tournament roster" in html


def test_a_match_that_started_30_minutes_ago_reads_started_never_in_started(client, login, db, cup):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-12T12:00:00Z")
    login(CAPTAIN)

    html = client.get("/").text
    script = client.get("/static/js/app.js").text

    # The wording travels with the timer: the script writes "in 02:00:00" or plain "Started".
    assert 'data-countdown-prefix="in "' in html
    assert not re.search(r"\bin\s*<time[^>]*data-countdown", html)
    assert 'prefix + formatCountdown(remaining) : "Started"' in script


def test_the_other_countdown_pages_carry_their_wording_the_same_way(client, login, db, cup):
    match_id = add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-20T19:00:00Z")
    login(CAPTAIN)

    for path in ("schedule", f"matches/{match_id}"):
        html = client.get(f"/t/{cup['id']}/{path}").text
        assert 'data-countdown-prefix="in "' in html, path
        assert not re.search(r"\bin\s*<time[^>]*data-countdown", html), path
