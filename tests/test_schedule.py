"""The schedule page, the best-slot finder page, pulling opponent scores, and the lineup filter
that keeps only players available at match time."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.repos import capture as capture_repo
from app.repos import tournaments as tournaments_repo
from app.services import score_capture as capture
from app.services.score_capture import PRIORITY_AUTOMATIC, PRIORITY_OPPONENT
from tests.support import (
    ADMIN_ID,
    add_availability,
    add_match,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_team,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER_PLAYER, SUB, GUEST, STRANGER = 2001, 2002, 2003, 2005, 2004, 2009
NEXT_MATCH = "2026-11-20T19:00:00Z"  # the test clock is 2026-11-12T12:30:15Z


def slot_run(first: str, count: int) -> list[str]:
    start = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return [
        (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(count)
    ]


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER_PLAYER, "player")
    add_member(db, tid, SUB, "player", is_substitute=True)
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid, "Round of 16", lineup_size=2)
    add_pool_map(db, stage, "NM1", beatmap_id=11)
    ours = add_team(
        db,
        tid,
        "Alpha Squad",
        [(CAPTAIN, "player2001"), (PLAYER, "player2002"), (OTHER_PLAYER, "player2003")],
        ours=True,
    )
    rival = add_team(
        db,
        tid,
        "Rival Crew",
        [
            (4001, "RivalRex", "Europe/Berlin"),
            (4002, "RivalRia", None, 330),
            (4003, "RivalRoy", None, None),
        ],
    )
    third = add_team(db, tid, "Third Wheel", [(5001, "ThirdTim", "Europe/Berlin")])
    return {"id": tid, "stage": stage, "ours": ours, "rival": rival, "third": third}


def schedule(client, cup):
    return client.get(f"/t/{cup['id']}/schedule")


# --- the schedule page -------------------------------------------------------------------------


def test_an_upcoming_match_shows_opponent_stage_time_and_a_countdown(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"], label="R16 M1")
    login(PLAYER)

    html = schedule(client, cup).text

    upcoming = html.split('id="upcoming-heading"')[1].split('id="past-heading"')[0]
    assert "R16 M1" in upcoming and "Alpha Squad vs Rival Crew" in upcoming
    assert ">Rival Crew<" in upcoming and ">Round of 16<" in upcoming
    assert f'datetime="{NEXT_MATCH}"' in upcoming
    assert "data-countdown" in upcoming


def test_the_opponent_is_the_other_team_whichever_side_we_are_on(client, login, cup, db):
    add_match(db, cup["id"], cup["rival"], cup["ours"], NEXT_MATCH, cup["stage"])
    login(PLAYER)

    upcoming = schedule(client, cup).text.split('id="past-heading"')[0]

    assert ">Rival Crew<" in upcoming and "Rival Crew vs Alpha Squad" in upcoming


def test_an_unscheduled_match_says_so(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["third"], None, cup["stage"], label="R16 M2")
    login(PLAYER)

    html = schedule(client, cup).text

    assert "Not scheduled yet" in html and "data-countdown" not in html


def test_upcoming_matches_are_in_time_order_with_the_unscheduled_last(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["third"], None, label="Later")
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-25T19:00:00Z", label="Second")
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, label="First")
    login(PLAYER)

    html = schedule(client, cup).text

    assert html.index("First") < html.index("Second") < html.index("Later")


def test_a_played_match_shows_the_result_from_our_side(client, login, cup, db):
    add_match(
        db, cup["id"], cup["ours"], cup["third"], "2026-11-10T18:00:00Z", None, "Q1", 5, 3, "done"
    )
    add_match(
        db, cup["id"], cup["rival"], cup["ours"], "2026-11-09T18:00:00Z", None, "Q2", 5, 2, "done"
    )
    login(PLAYER)

    played = schedule(client, cup).text.split('id="past-heading"')[1]

    assert re.search(r'result--win">W</span> <span class="num">5 - 3', played)  # we were team 1
    assert re.search(r'result--loss">L</span> <span class="num">2 - 5', played)  # we were team 2
    assert played.index("Q1") < played.index("Q2")  # most recent first


def test_a_match_whose_time_has_passed_is_listed_as_played_even_without_a_result(
    client, login, cup, db
):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-12T10:00:00Z", None, "Today")
    login(PLAYER)

    html = schedule(client, cup).text

    assert "No result yet" in html.split('id="past-heading"')[1]
    assert "Today" not in html.split('id="past-heading"')[0].split('id="upcoming-heading"')[1]


def test_only_matches_of_our_team_are_listed(client, login, cup, db):
    add_match(db, cup["id"], cup["rival"], cup["third"], NEXT_MATCH, None, "Not ours")
    login(PLAYER)

    html = schedule(client, cup).text

    assert "Not ours" not in html and "No upcoming matches are listed" in html


def test_without_our_team_every_match_is_listed_with_a_note(client, login, cup, db):
    db.execute("UPDATE tournaments SET our_team_id = NULL WHERE id = ?", (cup["id"],))
    add_match(db, cup["id"], cup["rival"], cup["third"], NEXT_MATCH, None, "Everyone")
    login(PLAYER)

    html = schedule(client, cup).text

    assert "Everyone" in html and "No team is marked as yours" in html


def test_empty_states_say_what_to_do_next(client, login, cup):
    login(PLAYER)

    html = schedule(client, cup).text

    assert "No upcoming matches are listed" in html and "No match has been played yet" in html
    assert "after the next wybin sync" not in html  # a hand-made tournament has no wybin source


def test_a_wybin_tournament_points_to_the_sync(client, login, cup, db):
    db.execute("UPDATE tournaments SET source = 'wybin' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    assert "after the next wybin sync" in schedule(client, cup).text


def test_a_stranger_gets_not_found_and_a_guest_may_read(client, login, cup):
    login(STRANGER)
    assert schedule(client, cup).status_code == 404
    login(GUEST)
    assert schedule(client, cup).status_code == 200


def test_find_a_slot_is_offered_for_matches_that_have_not_been_played(client, login, cup, db):
    open_match = add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, label="Open")
    done = add_match(
        db, cup["id"], cup["ours"], cup["third"], "2026-11-10T18:00:00Z", None, "Done", 5, 3, "done"
    )
    login(PLAYER)

    html = schedule(client, cup).text

    assert f"/schedule/slots?match={open_match}" in html
    assert f"/schedule/slots?match={done}" not in html


def test_a_match_without_a_decided_opponent_has_no_slot_finder(client, login, cup, db):
    undecided = add_match(db, cup["id"], cup["ours"], None, None, label="TBD")
    login(PLAYER)

    assert f"match={undecided}" not in schedule(client, cup).text


def test_only_captains_see_the_pull_scores_button(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH)
    login(PLAYER)
    assert "Pull scores" not in schedule(client, cup).text
    login(CAPTAIN)

    html = schedule(client, cup).text

    assert f'action="/t/{cup["id"]}/opponents/{cup["rival"]}/capture"' in html
    assert 'name="return_to" value="schedule"' in html


def test_pull_scores_is_disabled_in_an_archived_tournament(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH)
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    assert re.search(r'<button class="btn btn--text"[^>]* disabled', schedule(client, cup).text)


def test_pulling_from_the_schedule_comes_back_to_the_schedule(client, login, csrf, cup, db):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/opponents/{cup['rival']}/capture",
        data={**csrf(), "return_to": "schedule"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{cup['id']}/schedule"
    # The pull runs at once (a background task), so the three players are already logged.
    pulled = db.execute("SELECT osu_id FROM capture_log ORDER BY osu_id").fetchall()
    assert [row["osu_id"] for row in pulled] == [4001, 4002, 4003]


@pytest.mark.parametrize("value", ["", "elsewhere", "https://evil.example/", "//evil.example"])
def test_any_other_return_target_goes_to_the_overview(client, login, csrf, cup, value):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/opponents/{cup['rival']}/capture",
        data={**csrf(), "return_to": value},
        follow_redirects=False,
    )

    assert response.headers["location"] == f"/t/{cup['id']}"


# --- the slot finder ---------------------------------------------------------------------------


@pytest.fixture
def match_id(cup, db):
    return add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])


def slots_page(client, cup, match, **headers):
    return client.get(f"/t/{cup['id']}/schedule/slots", params={"match": match}, headers=headers)


def test_the_finder_ranks_starts_by_our_availability(client, login, cup, db, match_id):
    # Pia and Paul are free 17:00 to 19:30 on the 14th; only Pia on the 15th.
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-14T17:00:00Z", 6))
    add_availability(db, cup["id"], OTHER_PLAYER, slot_run("2026-11-14T17:00:00Z", 6))
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-15T17:00:00Z", 6))
    login(CAPTAIN)

    html = slots_page(client, cup, match_id).text

    rows = re.findall(r'<th scope="row">([^<]+)</th>', html.split("<tbody>")[1])
    assert rows[0].startswith("Sat 14 Nov 17:00") or rows[0].startswith("Sat 14 Nov 17:30")
    assert all(row.startswith("Sat 14 Nov") for row in rows[:3])
    assert "Sun 15 Nov" in " ".join(rows)
    assert "2 yes" in html and "Best times against Rival Crew" in html


def test_the_finder_shows_each_opponents_local_time_and_flags_the_unreasonable_ones(
    client, login, cup, db, match_id
):
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-14T18:00:00Z", 3))
    login(CAPTAIN)

    html = slots_page(client, cup, match_id).text

    assert "RivalRex: Sat 14 Nov 19:00 <span" in html and "(Europe/Berlin)" in html
    assert "RivalRia: Sat 14 Nov 23:30" in html and "(UTC+5:30)" in html
    assert "outside 10:00 to 23:00" in html
    assert "RivalRoy" in html and "timezone unknown" in html


def test_the_finder_says_so_when_nobody_is_free(client, login, cup, match_id):
    login(CAPTAIN)

    html = slots_page(client, cup, match_id).text

    assert "has a player free for the whole match" in html and "<tbody>" not in html


def test_the_finder_ignores_substitutes_guests_and_other_tournaments(
    client, login, cup, db, match_id
):
    add_availability(db, cup["id"], SUB, slot_run("2026-11-14T18:00:00Z", 3))
    add_availability(db, cup["id"], GUEST, slot_run("2026-11-14T18:00:00Z", 3))
    elsewhere = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, elsewhere, PLAYER, "player")
    add_availability(db, elsewhere, PLAYER, slot_run("2026-11-14T18:00:00Z", 3))
    login(CAPTAIN)

    assert "<tbody>" not in slots_page(client, cup, match_id).text


def test_the_window_runs_two_weeks_from_the_next_slot(client, login, cup, db, match_id):
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-25T18:00:00Z", 3))  # inside
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-26T18:00:00Z", 3))  # past the end
    login(CAPTAIN)

    html = slots_page(client, cup, match_id).text

    assert "Wed 25 Nov 18:00" in html and "Thu 26 Nov" not in html


def test_the_window_stops_at_the_end_of_the_stage(client, login, cup, db, match_id):
    db.execute("UPDATE stages SET end_at = '2026-11-14T00:00:00Z' WHERE id = ?", (cup["stage"],))
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-13T18:00:00Z", 3))
    add_availability(db, cup["id"], PLAYER, slot_run("2026-11-15T18:00:00Z", 3))
    login(CAPTAIN)

    html = slots_page(client, cup, match_id).text

    assert "Fri 13 Nov 18:00" in html and "Sun 15 Nov" not in html


def test_htmx_gets_a_fragment_and_a_browser_gets_the_page(client, login, cup, match_id):
    login(PLAYER)

    fragment = slots_page(client, cup, match_id, **{"HX-Request": "true"}).text
    full = slots_page(client, cup, match_id).text

    assert "<html" not in fragment and 'id="slots-heading"' in fragment
    assert "<html" in full and 'id="slots-heading"' in full and "Back to the schedule" in full


def test_the_finder_refuses_a_match_that_is_not_ours_to_look_at(client, login, cup, db, match_id):
    other = add_tournament(db, "Other Cup", acronym="OC")
    foreign = add_match(db, other, None, None, NEXT_MATCH)
    login(PLAYER)

    assert slots_page(client, cup, foreign).status_code == 404
    assert slots_page(client, cup, 999).status_code == 404
    assert slots_page(client, cup, "abc").status_code == 404
    assert slots_page(client, cup, "").status_code == 404


def test_the_finder_refuses_a_played_match_and_one_without_an_opponent(client, login, cup, db):
    played = add_match(
        db, cup["id"], cup["ours"], cup["third"], "2026-11-10T18:00:00Z", None, "Done", 5, 3, "done"
    )
    undecided = add_match(db, cup["id"], cup["ours"], None, None)
    login(PLAYER)

    assert slots_page(client, cup, played).status_code == 409
    assert slots_page(client, cup, undecided).status_code == 409


def test_the_finder_is_for_members_only(client, login, cup, match_id):
    login(STRANGER)

    assert slots_page(client, cup, match_id).status_code == 404


# --- capturing the next opponent ---------------------------------------------------------------


def next_opponent_queue(db):
    rows = db.execute(
        "SELECT osu_id, beatmap_id, priority FROM capture_queue ORDER BY priority, osu_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def tournament(db, cup):
    return tournaments_repo.get(db, cup["id"])


def test_the_next_opponents_players_are_queued_below_the_automatic_priority(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])

    queued = capture.enqueue_next_opponent(
        db, tournament(db, cup), clock.now(), timedelta(hours=24)
    )

    assert queued == 3
    assert next_opponent_queue(db) == [
        (4001, 11, PRIORITY_OPPONENT),
        (4002, 11, PRIORITY_OPPONENT),
        (4003, 11, PRIORITY_OPPONENT),
    ]
    assert PRIORITY_OPPONENT > PRIORITY_AUTOMATIC


def test_our_own_roster_is_pulled_before_the_opponent(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])

    capture.enqueue_active(db, clock, 24)

    order = [row["osu_id"] for row in capture_repo_batch(db)]
    assert set(order[:4]) == {CAPTAIN, PLAYER, OTHER_PLAYER, SUB}  # everyone but the guest
    assert set(order[4:]) == {4001, 4002, 4003}


def capture_repo_batch(db):
    return [
        {"osu_id": item.osu_id, "priority": item.priority}
        for item in capture_repo.next_batch(db, 50)
    ]


def test_the_opponent_is_the_team_of_the_earliest_unplayed_match(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["third"], "2026-11-25T19:00:00Z", cup["stage"])
    add_match(db, cup["id"], cup["rival"], cup["ours"], NEXT_MATCH, cup["stage"])

    assert capture.next_opponent_id(db, tournament(db, cup), clock.now()) == cup["rival"]


@pytest.mark.parametrize(
    "match",
    [
        {"scheduled_at": "2026-11-12T12:00:00Z"},  # already started
        {"scheduled_at": None},  # no time yet
        {"scheduled_at": NEXT_MATCH, "status": "done"},
        {"scheduled_at": NEXT_MATCH, "team2": None},  # opponent not decided
    ],
)
def test_matches_that_are_not_coming_up_queue_nobody(db, cup, clock, match):
    add_match(
        db,
        cup["id"],
        cup["ours"],
        match.get("team2", cup["rival"]),
        match["scheduled_at"],
        cup["stage"],
        status=match.get("status", "scheduled"),
    )

    assert (
        capture.enqueue_next_opponent(db, tournament(db, cup), clock.now(), timedelta(hours=24))
        == 0
    )
    assert next_opponent_queue(db) == []


def test_a_match_between_other_teams_queues_nobody(db, cup, clock):
    add_match(db, cup["id"], cup["rival"], cup["third"], NEXT_MATCH, cup["stage"])

    assert capture.next_opponent_id(db, tournament(db, cup), clock.now()) is None


def test_without_our_team_there_is_no_opponent(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])
    db.execute("UPDATE tournaments SET our_team_id = NULL WHERE id = ?", (cup["id"],))

    assert capture.next_opponent_id(db, tournament(db, cup), clock.now()) is None


def test_recently_pulled_opponent_players_are_skipped(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])
    db.execute(
        "INSERT INTO capture_log (osu_id, beatmap_id, gamemode, last_pulled_at) "
        "VALUES (4001, 11, 0, '2026-11-12T06:00:00Z')"
    )

    queued = capture.enqueue_next_opponent(
        db, tournament(db, cup), clock.now(), timedelta(hours=24)
    )

    assert queued == 2
    assert {row[0] for row in next_opponent_queue(db)} == {4002, 4003}


def test_a_person_asking_for_the_same_pull_keeps_the_urgent_priority(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])
    capture.enqueue_next_opponent(db, tournament(db, cup), clock.now(), timedelta(hours=24))

    capture.enqueue_for_team(
        db, tournament(db, cup), cup["rival"], capture.PRIORITY_REQUESTED, clock.now()
    )

    assert {row[2] for row in next_opponent_queue(db)} == {capture.PRIORITY_REQUESTED}


def test_the_hourly_enqueue_includes_the_opponent_and_a_second_run_adds_nothing(db, cup, clock):
    add_match(db, cup["id"], cup["ours"], cup["rival"], NEXT_MATCH, cup["stage"])
    add_score(db, 11, CAPTAIN, 900_000)

    first = capture.enqueue_active(db, clock, 24)
    db.execute("DELETE FROM capture_queue")
    for osu_id in (CAPTAIN, PLAYER, OTHER_PLAYER, SUB, 4001, 4002, 4003):
        db.execute(
            "INSERT OR REPLACE INTO capture_log (osu_id, beatmap_id, gamemode, last_pulled_at) "
            "VALUES (?, 11, 0, '2026-11-12T12:00:00Z')",
            (osu_id,),
        )
    second = capture.enqueue_active(db, clock, 24)

    assert first == 4 + 3  # our roster (no guest) and the opponent
    assert second == 0


# --- the lineup filter -------------------------------------------------------------------------


def lineup(client, cup, **params):
    return client.get(f"/t/{cup['id']}/lineup", params={"stage": cup["stage"], **params})


@pytest.fixture
def scored(db, cup):
    """Three players with scores on the one map, best to worst: Carl, Pia, Paul."""
    add_score(db, 11, CAPTAIN, 900_000)
    add_score(db, 11, PLAYER, 800_000)
    add_score(db, 11, OTHER_PLAYER, 700_000)
    return cup


def suggested_players(html: str) -> list[str]:
    section = html.split('id="suggestion-heading"')[1].split('id="saved-heading"')[0]
    return re.findall(r"<li>(player\d+) <span", section)


def test_without_the_filter_the_best_players_are_suggested(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    login(CAPTAIN)

    assert suggested_players(lineup(client, scored).text) == ["player2001", "player2002"]


def test_the_filter_keeps_only_players_who_said_yes_for_the_whole_match(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    add_availability(db, scored["id"], PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    add_availability(db, scored["id"], OTHER_PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    add_availability(db, scored["id"], CAPTAIN, slot_run("2026-11-20T19:00:00Z", 2))  # leaves early
    login(CAPTAIN)

    html = lineup(client, scored, avail="1").text

    assert suggested_players(html) == ["player2002", "player2003"]
    assert "Showing only players who answered Yes for the whole match" in html
    assert "Rival Crew" in html


def test_maybe_players_are_left_out_and_counted(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    add_availability(db, scored["id"], PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    add_availability(db, scored["id"], CAPTAIN, slot_run("2026-11-20T19:00:00Z", 3), "maybe")
    login(CAPTAIN)

    html = lineup(client, scored, avail="1").text

    assert suggested_players(html) == ["player2002"]
    assert "1 player answered Maybe and is left out." in html
    assert "Only 1 of 2 have a score." in html


def test_one_maybe_slot_in_the_middle_leaves_a_player_out(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    add_availability(db, scored["id"], PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    add_availability(db, scored["id"], CAPTAIN, slot_run("2026-11-20T19:00:00Z", 3))
    add_availability(db, scored["id"], CAPTAIN, ["2026-11-20T19:30:00Z"], "maybe")
    login(CAPTAIN)

    assert suggested_players(lineup(client, scored, avail="1").text) == ["player2002"]


def test_nobody_available_leaves_every_map_empty(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    login(CAPTAIN)

    html = lineup(client, scored, avail="1").text

    assert suggested_players(html) == [] and "Nobody has a score" in html


def test_substitutes_stay_out_unless_asked_for_even_when_available(client, login, db, scored):
    add_score(db, 11, SUB, 950_000)
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    for osu_id in (SUB, PLAYER):
        add_availability(db, scored["id"], osu_id, slot_run("2026-11-20T19:00:00Z", 3))
    login(CAPTAIN)

    assert suggested_players(lineup(client, scored, avail="1").text) == ["player2002"]
    with_subs = lineup(client, scored, avail="1", subs="1").text
    assert suggested_players(with_subs) == ["player2005", "player2002"]


def test_the_match_picker_chooses_which_match_time_counts(client, login, db, scored):
    first = add_match(
        db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"]
    )
    second = add_match(
        db, scored["id"], scored["ours"], scored["third"], "2026-11-27T19:00:00Z", scored["stage"]
    )
    add_availability(db, scored["id"], PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    add_availability(db, scored["id"], OTHER_PLAYER, slot_run("2026-11-27T19:00:00Z", 3))
    login(CAPTAIN)

    default = lineup(client, scored, avail="1").text
    chosen = lineup(client, scored, avail="1", match=str(second)).text

    assert suggested_players(default) == ["player2002"]  # the next match is the default
    assert suggested_players(chosen) == ["player2003"]
    assert f'<option value="{first}"' in default and f'<option value="{second}" selected' in chosen


def test_an_unknown_match_falls_back_to_the_next_one(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    add_availability(db, scored["id"], PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    login(CAPTAIN)

    html = lineup(client, scored, avail="1", match="999").text

    assert suggested_players(html) == ["player2002"]


def test_matches_of_other_stages_or_already_played_ones_are_not_offered(client, login, db, scored):
    other_stage = add_stage(db, scored["id"], "Finals", 2)
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, other_stage)
    add_match(
        db,
        scored["id"],
        scored["ours"],
        scored["third"],
        "2026-11-10T18:00:00Z",
        scored["stage"],
        score1=5,
        score2=3,
        status="done",
    )
    login(CAPTAIN)

    html = lineup(client, scored, avail="1").text

    assert 'name="avail"' not in html
    assert suggested_players(html) == ["player2001", "player2002"]  # no filter without a match


def test_the_filter_checkbox_appears_when_the_stage_has_an_upcoming_match(
    client, login, db, scored
):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    login(PLAYER)

    html = lineup(client, scored).text

    assert "Only players available at match time" in html and 'name="match"' in html


def test_the_choice_survives_copying_the_suggestion(client, login, csrf, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    add_availability(db, scored["id"], OTHER_PLAYER, slot_run("2026-11-20T19:00:00Z", 3))
    login(CAPTAIN)
    page = lineup(client, scored, avail="1").text
    assert 'name="avail" value="1"' in page  # the copy form repeats the choice

    response = client.post(
        f"/t/{scored['id']}/lineup/copy-suggestion",
        data={**csrf(), "stage": str(scored["stage"]), "avail": "1", "match": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303 and "avail=1" in response.headers["location"]
    saved = db.execute("SELECT osu_id FROM lineup_entries").fetchall()
    assert [row["osu_id"] for row in saved] == [OTHER_PLAYER]


def test_the_tournament_admin_can_use_the_filter(client, login, db, scored):
    add_match(db, scored["id"], scored["ours"], scored["rival"], NEXT_MATCH, scored["stage"])
    login(ADMIN_ID)

    assert "Only players available at match time" in lineup(client, scored).text
