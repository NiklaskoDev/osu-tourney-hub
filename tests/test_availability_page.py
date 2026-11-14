"""The availability page: who sees what, saving a range, UTC versus local, the match overlay."""

import dataclasses
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.support import (
    ADMIN_ID,
    add_availability,
    add_match,
    add_member,
    add_stage,
    add_team,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER_PLAYER, SUB, GUEST, STRANGER = 2001, 2002, 2003, 2005, 2004, 2009
WEEK = "2026-11-16"  # a Monday; the test clock is Thursday 2026-11-12


def slots(first: str, count: int) -> list[str]:
    start = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return [
        (start + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(count)
    ]


def stored(db, tid, osu_id) -> dict[str, str]:
    rows = db.execute(
        "SELECT slot_start, status FROM availability WHERE tournament_id = ? AND osu_id = ?",
        (tid, osu_id),
    )
    return {row["slot_start"]: row["status"] for row in rows}


def row_html(page: str, slot_text: str) -> str:
    epoch = int(datetime.strptime(slot_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
    match = re.search(rf'<tr id="slot-{epoch}".*?</tr>', page, re.DOTALL)
    assert match, f"no row for {slot_text}"
    return match.group(0)


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER_PLAYER, "player")
    add_member(db, tid, SUB, "player", is_substitute=True)
    add_member(db, tid, GUEST, "guest")
    db.execute("UPDATE users SET timezone = 'Europe/Berlin' WHERE osu_id = ?", (CAPTAIN,))
    stage = add_stage(db, tid, lineup_size=2)
    ours = add_team(db, tid, "Alpha Squad", [(CAPTAIN, "player2001")], ours=True)
    rival = add_team(db, tid, "Rival Crew", [(4001, "RivalRex", "Europe/Berlin")])
    return {"id": tid, "stage": stage, "ours": ours, "rival": rival}


def page(client, cup, **params):
    """The edit grid (`?edit=1`), which holds the slot rows and the buttons."""
    return client.get(f"/t/{cup['id']}/availability", params={"edit": "1", **params})


def view_page(client, cup, **params):
    """The default timeline view."""
    return client.get(f"/t/{cup['id']}/availability", params=params)


def post(client, csrf, cup, **fields):
    data = {
        **csrf(),
        "start": "2026-11-16T18:00:00Z",
        "end": "2026-11-16T18:00:00Z",
        "state": "yes",
    }
    return client.post(f"/t/{cup['id']}/availability", data={**data, **fields})


# --- reading the grid --------------------------------------------------------------------------


def test_the_grid_has_a_column_for_everyone_on_the_roster_but_guests(client, login, cup):
    login(PLAYER)

    html = page(client, cup, week=WEEK).text

    for name in ("player2001", "player2002", "player2003", "player2005"):
        assert f'title="{name}"' in html
    assert 'title="player2004"' not in html


def test_nobody_outside_the_tournament_sees_it(client, login, cup):
    login(STRANGER)

    assert page(client, cup).status_code == 404


def test_a_visitor_who_is_not_logged_in_is_sent_to_log_in(client, cup):
    response = client.get(f"/t/{cup['id']}/availability", follow_redirects=False)

    assert response.status_code == 303 and "/auth/login" in response.headers["location"]


def test_the_default_week_is_the_one_with_today_in_it(client, login, cup):
    login(PLAYER)

    assert "09 Nov - 15 Nov 2026" in page(client, cup).text


def test_week_links_move_by_a_week_and_keep_the_view(client, login, cup):
    login(PLAYER)

    html = page(client, cup, week=WEEK, tz="local", zone="Asia/Tokyo").text

    assert "week=2026-11-09" in html and "week=2026-11-23" in html


def test_the_players_own_column_has_buttons_that_say_their_state_to_screen_readers(
    client, login, cup, db
):
    add_availability(db, cup["id"], PLAYER, ["2026-11-16T18:00:00Z"], "maybe")
    login(PLAYER)

    html = page(client, cup, week=WEEK).text

    assert html.count('<button class="avail"') == 336
    yes_cell = row_html(html, "2026-11-16T18:00:00Z")
    assert 'aria-label="player2002, Mon 16 Nov 18:00: Maybe"' in yes_cell
    assert 'data-state="maybe"' in yes_cell
    assert 'aria-label="player2002, Mon 16 Nov 18:30: No"' in row_html(html, "2026-11-16T18:30:00Z")


def test_other_peoples_answers_are_text_not_controls(client, login, cup, db):
    add_availability(db, cup["id"], OTHER_PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(PLAYER)

    row = row_html(page(client, cup, week=WEEK).text, "2026-11-16T18:00:00Z")

    assert row.count("<button") == 1  # only the viewer's own cell
    assert row.count("avail--static") == 3
    assert '<span class="avail avail--static" data-state="yes">Yes</span>' in row


def test_the_summary_counts_yes_answers_against_the_lineup_size(client, login, cup, db):
    add_availability(db, cup["id"], PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    add_availability(db, cup["id"], OTHER_PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    add_availability(db, cup["id"], SUB, ["2026-11-16T18:00:00Z"], "maybe")
    login(PLAYER)

    html = page(client, cup, week=WEEK).text

    row = row_html(html, "2026-11-16T18:00:00Z")
    assert "2 / 2" in row and "+1 maybe" in row and "enough players" in row
    assert "0 / 2" in row_html(html, "2026-11-16T18:30:00Z")


def test_a_guest_can_look_but_has_no_controls(client, login, cup, db):
    add_availability(db, cup["id"], PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(GUEST)

    html = page(client, cup, week=WEEK).text

    assert '<button class="avail"' not in html
    assert "you can look but not change" in html


def test_a_captain_can_pick_whose_row_to_edit(client, login, cup):
    login(CAPTAIN)

    html = page(client, cup, week=WEEK, member=str(PLAYER)).text

    assert 'aria-label="player2002, Mon 16 Nov 18:00: No"' in html
    assert 'aria-label="player2001, Mon 16 Nov 18:00' not in html
    assert "You are editing player2002" in html
    assert 'name="member"' in html


def test_a_player_cannot_pick_someone_elses_row(client, login, cup):
    login(PLAYER)

    html = page(client, cup, week=WEEK, member=str(OTHER_PLAYER)).text

    assert 'aria-label="player2002, Mon 16 Nov 18:00' in html
    assert 'aria-label="player2003, Mon 16 Nov 18:00' not in html
    assert 'name="member"' not in html


def test_an_archived_tournament_shows_the_grid_with_disabled_cells(client, login, cup, db):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    html = page(client, cup, week=WEEK).text

    assert "This tournament is archived" in html
    assert 'data-slot="2026-11-16T18:00:00Z"' in html
    assert re.search(r'<button class="avail"[^>]* disabled>', html)


def test_the_grid_script_is_loaded_after_htmx_and_the_page_has_no_inline_script(client, login, cup):
    login(PLAYER)

    html = client.get(f"/t/{cup['id']}/availability").text

    assert html.index("htmx.min.js") < html.index("/static/js/availability.js")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert not re.search(r"\sstyle=", html), "inline style attribute"


def test_an_empty_roster_says_so(client, login, db):
    tid = add_tournament(db, "Empty Cup", acronym="EC")
    login(ADMIN_ID)

    html = client.get(f"/t/{tid}/availability").text

    assert "Nobody is on the roster yet" in html


# --- UTC and local -----------------------------------------------------------------------------


def test_utc_is_the_default_and_rows_carry_utc_times(client, login, cup):
    login(CAPTAIN)

    html = page(client, cup, week=WEEK).text

    assert "Time (UTC)" in html
    assert ">18:00<" in row_html(html, "2026-11-16T18:00:00Z")


def test_local_uses_the_saved_timezone_and_shows_the_local_clock(client, login, cup):
    login(CAPTAIN)  # saved timezone Europe/Berlin, UTC+1 in November

    html = page(client, cup, week=WEEK, tz="local").text

    assert "Time (Europe/Berlin)" in html and "Local (Europe/Berlin)" in html
    row = row_html(html, "2026-11-16T19:00:00Z")
    assert re.search(r'<th scope="row" class="slot__time">20:00', row)
    assert "Mon 16 Nov 20:00: No" in row  # the spoken label is local too
    assert 'data-zone="Europe/Berlin"' in html


def test_a_local_week_starts_at_local_midnight(client, login, cup):
    login(CAPTAIN)

    html = page(client, cup, week=WEEK, tz="local").text

    first = re.search(r'<tr id="slot-\d+"', html)
    assert first
    assert 'data-slot="2026-11-15T23:00:00Z"' in html  # Berlin's Monday 00:00
    assert "Monday 16 November" in html


def test_local_without_a_saved_timezone_uses_the_browsers_zone(client, login, cup):
    login(PLAYER)  # no saved timezone

    html = page(client, cup, week=WEEK, tz="local", zone="Asia/Tokyo").text

    assert "Time (Asia/Tokyo)" in html
    assert "data-detect-zone" not in html
    assert ">03:00<" in row_html(html, "2026-11-16T18:00:00Z")  # UTC+9


def test_local_without_any_zone_falls_back_to_utc_and_asks_the_browser(client, login, cup):
    login(PLAYER)

    html = page(client, cup, week=WEEK, tz="local").text

    assert "Time (UTC)" in html
    assert "data-detect-zone" in html and "Your timezone is not saved yet" in html


@pytest.mark.parametrize("zone", ["Mars/Olympus", "../etc/passwd", "US/Eastern", "<script>"])
def test_a_made_up_zone_is_ignored(client, login, cup, zone):
    login(PLAYER)

    html = page(client, cup, week=WEEK, tz="local", zone=zone).text

    assert "Time (UTC)" in html and "data-detect-zone" in html
    assert zone not in html.replace("&lt;script&gt;", "")


def test_the_week_the_clocks_go_back_has_338_local_rows(client, login, cup):
    login(CAPTAIN)

    html = page(client, cup, week="2026-10-19", tz="local").text

    assert html.count('<tr id="slot-') == 338
    assert html.count('data-slot="') == 338


# --- the match overlay -------------------------------------------------------------------------


def test_our_match_shades_its_rows_and_is_named_once(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-16T19:30:00Z", cup["stage"])
    login(PLAYER)

    html = page(client, cup, week=WEEK).text

    shaded = re.findall(r'<tr id="slot-\d+" class="slot slot--match">', html)
    assert len(shaded) == 3
    assert html.count("Match vs Rival Crew") == 1
    assert "Match vs Rival Crew" in row_html(html, "2026-11-16T19:30:00Z")
    assert "slot--match" not in row_html(html, "2026-11-16T19:00:00Z")
    assert "slot--match" in row_html(html, "2026-11-16T20:30:00Z")
    assert "slot--match" not in row_html(html, "2026-11-16T21:00:00Z")


def test_the_overlay_follows_the_local_clock(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-16T19:30:00Z", cup["stage"])
    login(CAPTAIN)

    html = page(client, cup, week=WEEK, tz="local").text

    row = row_html(html, "2026-11-16T19:30:00Z")
    assert "slot--match" in row and re.search(r'slot__time">20:30', row)


def test_matches_of_other_teams_are_not_drawn(client, login, cup, db):
    third = add_team(db, cup["id"], "Third Wheel")
    add_match(db, cup["id"], cup["rival"], third, "2026-11-16T19:30:00Z", cup["stage"])
    login(PLAYER)

    assert "slot--match" not in page(client, cup, week=WEEK).text


def test_an_unscheduled_match_is_not_drawn(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], None)
    login(PLAYER)

    assert "slot--match" not in page(client, cup, week=WEEK).text


def test_a_match_that_started_before_the_week_still_shades_its_first_slot(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-15T23:30:00Z")  # Sunday night
    login(PLAYER)

    html = page(client, cup, week=WEEK).text

    assert "slot--match" in row_html(html, "2026-11-16T00:00:00Z")
    assert "slot--match" not in row_html(html, "2026-11-16T01:00:00Z")


# --- saving ------------------------------------------------------------------------------------


def test_a_player_saves_a_range_of_their_own_slots(client, login, csrf, cup, db):
    login(PLAYER)

    response = post(
        client, csrf, cup, start="2026-11-16T18:00:00Z", end="2026-11-16T19:00:00Z", state="yes"
    )

    assert response.status_code == 200
    assert stored(db, cup["id"], PLAYER) == {
        "2026-11-16T18:00:00Z": "yes",
        "2026-11-16T18:30:00Z": "yes",
        "2026-11-16T19:00:00Z": "yes",
    }
    assert "Saved 3 slots as Yes." in response.text


def test_the_answer_carries_the_changed_rows_for_htmx_to_swap_in(client, login, csrf, cup):
    login(PLAYER)

    html = post(client, csrf, cup, state="maybe").text

    assert "<template>" in html
    row = row_html(html, "2026-11-16T18:00:00Z")
    assert 'hx-swap-oob="true"' in row
    assert 'data-state="maybe"' in row
    assert 'aria-label="player2002, Mon 16 Nov 18:00: Maybe"' in row
    assert "Saved 1 slot as Maybe." in html


def test_a_new_answer_replaces_the_old_one_and_no_clears_it(client, login, csrf, cup, db):
    login(PLAYER)
    post(client, csrf, cup, state="yes")
    post(client, csrf, cup, state="maybe")
    assert stored(db, cup["id"], PLAYER) == {"2026-11-16T18:00:00Z": "maybe"}

    post(client, csrf, cup, state="no")

    assert stored(db, cup["id"], PLAYER) == {}


def test_the_updated_row_counts_everyone(client, login, csrf, cup, db):
    add_availability(db, cup["id"], OTHER_PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(PLAYER)

    row = row_html(post(client, csrf, cup, state="yes").text, "2026-11-16T18:00:00Z")

    assert "2 / 2" in row


def test_a_range_picked_on_the_local_grid_is_stored_in_utc(client, login, csrf, cup, db):
    login(CAPTAIN)  # Berlin, UTC+1
    grid = page(client, cup, week=WEEK, tz="local").text
    # The cell the viewer sees at local 20:00 on Monday is the slot at 19:00 UTC.
    cell = re.search(r'data-slot="([^"]+)" data-label="player2001, Mon 16 Nov 20:00"', grid)
    assert cell

    response = post(
        client, csrf, cup, start=cell.group(1), end=cell.group(1), state="yes", tz="local"
    )

    assert stored(db, cup["id"], CAPTAIN) == {"2026-11-16T19:00:00Z": "yes"}
    assert re.search(r'slot__time">20:00', row_html(response.text, "2026-11-16T19:00:00Z"))


def test_changes_stay_inside_the_tournament(client, login, csrf, cup, db):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, PLAYER, "player")
    add_availability(db, other, PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(PLAYER)

    post(client, csrf, cup, state="no")

    assert stored(db, other, PLAYER) == {"2026-11-16T18:00:00Z": "yes"}


@pytest.mark.parametrize(
    "fields",
    [
        {"state": "perhaps"},
        {"state": ""},
        {"start": "2026-11-16T18:15:00Z", "end": "2026-11-16T18:15:00Z"},
        {"start": "2026-11-16T19:00:00Z", "end": "2026-11-16T18:00:00Z"},
        {"start": "2026-11-16T00:00:00Z", "end": "2026-11-30T00:00:00Z"},
        {"start": "", "end": ""},
        {"start": "garbage", "end": "garbage"},
    ],
)
def test_a_bad_range_or_state_changes_nothing(client, login, csrf, cup, db, fields):
    login(PLAYER)

    response = post(client, csrf, cup, **fields)

    assert response.status_code == 422
    assert stored(db, cup["id"], PLAYER) == {}


def test_a_player_cannot_change_someone_elses_answers(client, login, csrf, cup, db):
    login(PLAYER)

    response = post(client, csrf, cup, osu_id=str(OTHER_PLAYER))

    assert response.status_code == 403
    assert stored(db, cup["id"], OTHER_PLAYER) == {}


def test_a_captain_changes_a_players_answers(client, login, csrf, cup, db):
    login(CAPTAIN)

    response = post(client, csrf, cup, osu_id=str(PLAYER), state="maybe")

    assert response.status_code == 200
    assert stored(db, cup["id"], PLAYER) == {"2026-11-16T18:00:00Z": "maybe"}
    assert stored(db, cup["id"], CAPTAIN) == {}
    assert 'aria-label="player2002, Mon 16 Nov 18:00: Maybe"' in response.text


def test_a_temporary_captain_may_change_others(client, login, csrf, cup, db):
    add_member(db, cup["id"], 2007, "player", temp_captain_until="2099-01-01T00:00:00Z")
    login(2007)

    assert post(client, csrf, cup, osu_id=str(PLAYER)).status_code == 200


@pytest.mark.parametrize("target", [str(GUEST), str(STRANGER), "abc", "-1", "9" * 40])
def test_a_captain_cannot_set_availability_for_a_non_player(client, login, csrf, cup, db, target):
    login(CAPTAIN)

    response = post(client, csrf, cup, osu_id=target)

    assert response.status_code == 422
    assert db.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def test_a_guest_cannot_save(client, login, csrf, cup, db):
    login(GUEST)

    assert post(client, csrf, cup).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def test_a_stranger_gets_not_found(client, login, csrf, cup, db):
    login(STRANGER)

    assert post(client, csrf, cup).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def test_an_admin_outside_the_roster_has_no_row_of_their_own(client, login, csrf, cup, db):
    login(ADMIN_ID)

    response = post(client, csrf, cup)

    assert response.status_code == 409
    assert "roster" in response.text
    assert db.execute("SELECT COUNT(*) FROM availability").fetchone()[0] == 0


def test_an_admin_may_change_a_players_answers(client, login, csrf, cup, db):
    login(ADMIN_ID)

    assert post(client, csrf, cup, osu_id=str(PLAYER)).status_code == 200
    assert stored(db, cup["id"], PLAYER) == {"2026-11-16T18:00:00Z": "yes"}


def test_an_archived_tournament_refuses_changes(client, login, csrf, cup, db):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    response = post(client, csrf, cup)

    assert response.status_code == 409
    assert stored(db, cup["id"], PLAYER) == {}


def test_saving_needs_the_csrf_token(client, login, cup, db):
    login(PLAYER)

    response = client.post(
        f"/t/{cup['id']}/availability",
        data={"start": "2026-11-16T18:00:00Z", "end": "2026-11-16T18:00:00Z", "state": "yes"},
    )

    assert response.status_code == 403
    assert stored(db, cup["id"], PLAYER) == {}


def test_htmx_may_send_the_token_as_a_header(client, login, csrf, cup, db):
    login(PLAYER)
    token = csrf()["csrf_token"]

    response = client.post(
        f"/t/{cup['id']}/availability",
        data={"start": "2026-11-16T18:00:00Z", "end": "2026-11-16T18:00:00Z", "state": "yes"},
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
    )

    assert response.status_code == 200
    assert stored(db, cup["id"], PLAYER) == {"2026-11-16T18:00:00Z": "yes"}


def test_removing_a_member_removes_their_answers_there(client, login, csrf, cup, db):
    add_availability(db, cup["id"], PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    add_availability(db, cup["id"], OTHER_PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/members/{PLAYER}/remove", data=csrf(), follow_redirects=False
    )

    assert response.status_code == 303
    assert stored(db, cup["id"], PLAYER) == {}
    assert stored(db, cup["id"], OTHER_PLAYER) == {"2026-11-16T18:00:00Z": "yes"}


# --- the timeline view (the default) -------------------------------------------------------------


def timeline_rows(html: str) -> list[str]:
    return re.findall(r'<tr class="timeline__row[^"]*">.*?</tr>', html, re.DOTALL)


def test_the_default_view_is_a_timeline_per_day_without_slot_buttons(client, login, cup):
    login(PLAYER)

    html = view_page(client, cup, week=WEEK).text

    assert html.count('<table class="timeline">') == 7
    assert 'role="region" aria-label="Availability on Monday 16 Nov"' in html
    assert "data-slot=" not in html
    assert 'class="avail"' not in html
    assert "<h1>Availability</h1>" in html


def test_every_timeline_row_adds_up_to_the_slot_count(client, login, cup, db):
    add_availability(db, cup["id"], PLAYER, slots("2026-11-16T18:00:00Z", 6))
    add_availability(db, cup["id"], OTHER_PLAYER, slots("2026-11-17T12:00:00Z", 3), "maybe")
    login(PLAYER)

    html = view_page(client, cup, week=WEEK).text

    rows = timeline_rows(html)
    assert len(rows) == 7 * 4  # seven days, four players (the guest has no row)
    for row in rows:
        assert sum(int(span) for span in re.findall(r'<td colspan="(\d+)"', row)) == 48


def test_a_player_with_a_saved_slot_gets_the_check_and_one_without_is_dashed(
    client, login, cup, db
):
    add_availability(db, cup["id"], PLAYER, ["2026-11-16T18:00:00Z"])
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK).text

    monday = html.split('aria-label="Availability on Tuesday 17 Nov"')[0]
    by_name = {
        name: row
        for row in timeline_rows(monday)
        for name in ("player2001", "player2002", "player2003")
        if f'title="{name}"' in row
    }
    assert "filled in" in by_name["player2002"] and "is-unfilled" not in by_name["player2002"]
    assert "filled in" not in by_name["player2003"] and "is-unfilled" in by_name["player2003"]


def test_the_check_only_counts_the_shown_week(client, login, cup, db):
    add_availability(db, cup["id"], PLAYER, ["2026-11-23T18:00:00Z"])  # the week after
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK).text

    assert ">filled in<" not in html


def test_the_bars_say_their_state_and_time_and_the_row_header_says_it_in_words(
    client, login, cup, db
):
    add_availability(db, cup["id"], PLAYER, slots("2026-11-16T18:00:00Z", 4))
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK).text

    assert 'data-state="yes" title="Available 18:00-20:00"' in html
    assert "Yes 18:00-20:00, No otherwise" in html


def test_a_match_is_a_labelled_band_on_its_day(client, login, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-11-17T19:30:00Z")
    login(PLAYER)

    html = view_page(client, cup, week=WEEK).text

    assert html.count('<span class="timeline__band"') == 1  # on the match's day only
    assert ">vs Rival Crew<" in html
    assert html.count('class="timeline__matches"') == 1


def test_update_my_availability_opens_the_edit_mode_for_a_player(client, login, cup):
    login(PLAYER)

    html = view_page(client, cup, week=WEEK).text

    assert "Update my availability" in html
    assert f'href="/t/{cup["id"]}/availability?week={WEEK}&amp;edit=1"' in html


@pytest.mark.parametrize("who", [GUEST, STRANGER])
def test_nobody_without_a_player_row_gets_the_update_button(client, login, cup, db, who):
    if who == STRANGER:
        add_member(db, cup["id"], who, "guest")
    login(who)

    html = view_page(client, cup, week=WEEK).text

    assert "Update my availability" not in html
    assert "You are not on the roster as a player" in html


def test_an_archived_tournament_has_no_update_button(client, login, cup, db):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    html = view_page(client, cup, week=WEEK).text

    assert "Update my availability" not in html
    assert html.count('<table class="timeline">') == 7


def test_the_zone_switch_is_on_for_local_and_off_for_utc(client, login, cup):
    login(CAPTAIN)

    utc_html = view_page(client, cup, week=WEEK).text
    local_html = view_page(client, cup, week=WEEK, tz="local").text

    assert re.search(
        r'<input class="switch__input"[^>]*name="tz"[^>]*value="local" checked', local_html
    )
    assert not re.search(r'<input class="switch__input"[^>]* checked', utc_html)
    assert "data-autosubmit" in utc_html and "<noscript>" in utc_html


def test_the_share_link_keeps_the_view_but_not_the_member_or_the_edit_mode(client, login, cup):
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK, member=PLAYER, edit="0", tz="utc").text

    match = re.search(r'data-copy="([^"]+)"', html)
    assert match
    link = match.group(1).replace("&amp;", "&")
    assert link.startswith("https://tourney.example.com/t/")
    assert f"week={WEEK}" in link and "member" not in link and "edit" not in link
    assert "Link copied" in html


def test_the_timezone_script_still_runs_in_the_timeline_view(client, login, cup, db):
    db.execute("UPDATE users SET timezone = NULL WHERE osu_id = ?", (CAPTAIN,))
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK, tz="local").text

    assert "/static/js/availability.js" in html
    assert "data-detect-zone" in html


# --- the edit mode --------------------------------------------------------------------------------


def test_edit_mode_has_the_grid_with_buttons_only_in_the_own_column_and_a_done_link(
    client, login, cup
):
    login(PLAYER)

    html = client.get(f"/t/{cup['id']}/availability", params={"edit": "1", "week": WEEK}).text

    assert "<h1>Update availability</h1>" in html
    assert 'class="timeline"' not in html
    assert html.count('data-slot="2026-11-16T18:00:00Z"') == 1
    assert f'href="/t/{cup["id"]}/availability?week={WEEK}"' in html  # Done


def test_edit_mode_lets_a_captain_choose_whose_column_to_edit(client, login, cup):
    login(CAPTAIN)

    html = client.get(
        f"/t/{cup['id']}/availability", params={"edit": "1", "week": WEEK, "member": PLAYER}
    ).text

    assert 'data-member="2002"' in html
    assert re.search(r'<form class="toolbar" method="get"[^>]*data-autosubmit novalidate>', html)
    assert 'name="edit" value="1"' in html


def test_the_share_link_falls_back_to_the_address_of_the_request_without_a_public_url(
    client, app, login, cup
):
    app.state.settings = dataclasses.replace(app.state.settings, public_url="")
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK).text

    link = re.search(r'data-copy="([^"]+)"', html).group(1)
    assert link.startswith("https://tourney.example.com/t/")
    assert link.endswith(f"/availability?week={WEEK}")


def test_a_one_slot_free_run_shows_the_count_against_the_lineup_size(client, login, cup, db):
    add_availability(db, cup["id"], PLAYER, ["2026-11-16T18:00:00Z"])
    login(CAPTAIN)

    html = view_page(client, cup, week=WEEK).text

    assert re.search(r'<td colspan="1" class="">1/2', html)


def test_the_share_link_keeps_the_time_zone_of_the_view_and_says_so(client, login, cup):
    login(CAPTAIN)  # saved timezone: Europe/Berlin

    html = view_page(client, cup, week=WEEK, tz="local").text

    link = re.search(r'data-copy="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert "tz=local" in link and "zone=Europe%2FBerlin" in link and f"week={WEEK}" in link
    assert (
        "The share link opens in the time zone you are viewing; whoever opens it can switch."
        in html
    )


def test_the_share_link_of_a_utc_view_has_no_zone(client, login, cup):
    login(CAPTAIN)

    link = re.search(r'data-copy="([^"]+)"', view_page(client, cup, week=WEEK).text).group(1)

    assert "zone" not in link and "tz=" not in link
