"""The match page, the captain's plan form, the pickems page and the qualifier seed line."""

import re
from datetime import timedelta

import pytest

from tests.matchup_support import CAPTAIN, GUEST, MATE, add_game, build_cup, fill_scores
from tests.support import ADMIN_ID, add_member, add_team, add_tournament

STRANGER = 2009


@pytest.fixture
def cup(db):
    return build_cup(db)


@pytest.fixture
def match_id(db, cup):
    fill_scores(db)
    return add_game(db, cup)


def url(cup, match, tail=""):
    return f"/t/{cup['id']}/matches/{match}{tail}"


def plan_rows(db):
    rows = db.execute("SELECT kind, pool_map_id, position, note FROM plan_items ORDER BY id")
    return [tuple(row) for row in rows]


def snapshot_count(db):
    return db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]


def post_plan(client, token, cup, match, **fields):
    return client.post(url(cup, match, "/plan"), data={**token, **fields}, follow_redirects=False)


def selected(page, name):
    """The value chosen in the select called `name`."""
    block = re.search(rf'<select[^>]*name="{name}".*?</select>', page, re.S).group(0)
    chosen = re.search(r'<option value="(\d*)" selected', block)
    return chosen.group(1) if chosen else ""


# --- the page ----------------------------------------------------------------------------------


def test_a_member_sees_the_comparison_the_prediction_and_the_plan_form(
    client, login, cup, match_id
):
    login(CAPTAIN)

    page = client.get(url(cup, match_id))

    assert page.status_code == 200
    text = page.text
    assert "Alpha Squad vs Rival Crew" in text
    assert "Chance for Alpha Squad" in text
    assert "Most likely score" in text
    assert re.search(
        r'Maps with data</dt><dd><span class="num">4</span> <span class="muted">of 4', text
    )
    assert "Predicted protects, bans and picks" in text
    assert "Alpha Squad picks first" in text and "Rival Crew picks first" in text
    assert 'name="ban_1"' in text and 'name="pick_1"' in text
    assert 'name="protect_1"' not in text  # this stage has no protects
    assert "Pull scores" in text


def test_a_win_chance_carries_a_letter_as_well_as_the_number(client, login, db, cup, match_id):
    login(CAPTAIN)
    ahead = r'result--win">W</span> <span class="num">\d+%'
    behind = r'result--loss">L</span> <span class="num">\d+%'

    assert re.search(ahead, client.get(url(cup, match_id)).text)

    db.execute("DELETE FROM scores")
    fill_scores(db, ours=300_000, theirs=900_000)

    behind_page = client.get(url(cup, match_id)).text
    assert re.search(behind, behind_page) and not re.search(ahead, behind_page)


def test_a_non_member_and_a_foreign_or_unknown_match_are_not_found(
    client, login, db, cup, match_id
):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, STRANGER, "player")

    login(STRANGER)
    assert client.get(url(cup, match_id)).status_code == 404

    login(CAPTAIN)
    for tail in (str(match_id + 100), "abc", "-1"):
        assert client.get(f"/t/{cup['id']}/matches/{tail}").status_code == 404, tail
    assert client.get(f"/t/{other}/matches/{match_id}").status_code == 404


def test_a_match_without_a_decided_opponent_explains_it(client, login, db, cup):
    undecided = db.execute(
        "INSERT INTO matches (tournament_id, stage_id, team1_id, status, updated_at) "
        "VALUES (?, ?, ?, 'scheduled', '2026-11-01T00:00:00Z')",
        (cup["id"], cup["stage"], cup["ours"]),
    ).lastrowid
    login(CAPTAIN)

    page = client.get(url(cup, undecided))

    assert page.status_code == 200
    assert "not decided yet" in page.text
    assert "Map by map" not in page.text and snapshot_count(db) == 0


def test_a_match_without_scores_says_so_and_still_lists_the_maps(client, login, db, cup):
    match = add_game(db, cup)
    login(CAPTAIN)

    page = client.get(url(cup, match)).text

    assert "No scores yet for both teams on any map." in page
    assert page.count("No data") >= 4  # one per map in the table
    assert snapshot_count(db) == 0


def test_an_estimated_score_is_labelled(client, login, db, cup, match_id):
    db.execute("DELETE FROM scores WHERE osu_id = 3002 AND beatmap_id = 1002")
    login(CAPTAIN)

    page = client.get(url(cup, match_id)).text

    assert 'class="cell-sub muted">estimated' in page
    assert "<td>Estimated</td>" in page


def test_a_player_reads_the_plan_but_has_no_form(client, login, csrf, db, cup, match_id):
    login(CAPTAIN)
    saved = post_plan(
        client,
        csrf(),
        cup,
        match_id,
        ban_1=str(cup["maps"]["HD1"]),
        ban_note_1="They always pick it",
        pick_1=str(cup["maps"]["NM1"]),
    )
    assert saved.status_code == 303

    for reader in (MATE, GUEST):
        login(reader)
        page = client.get(url(cup, match_id)).text
        assert "Our planned protects, bans and picks" in page
        assert "They always pick it" in page and "Ban 1" in page and "Pick 1" in page
        assert 'name="ban_1"' not in page and "Save plan" not in page


def test_a_player_sees_an_empty_plan_as_such(client, login, cup, match_id):
    login(MATE)

    page = client.get(url(cup, match_id)).text

    assert "No plan has been made for this match yet." in page
    assert "Save plan" not in page


def test_the_plan_is_not_shown_for_a_match_we_do_not_play(client, login, db, cup):
    other = add_team(db, cup["id"], "Third Team", [(3101, "ThirdA"), (3102, "ThirdB")])
    foreign = db.execute(
        "INSERT INTO matches (tournament_id, stage_id, team1_id, team2_id, status, updated_at) "
        "VALUES (?, ?, ?, ?, 'scheduled', '2026-11-01T00:00:00Z')",
        (cup["id"], cup["stage"], cup["rivals"], other),
    ).lastrowid
    login(CAPTAIN)

    page = client.get(url(cup, foreign)).text

    assert "Rival Crew vs Third Team" in page
    assert "Our plan" not in page and "Pull scores" not in page


def test_an_archived_tournament_shows_the_plan_read_only(client, login, db, cup, match_id):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    page = client.get(url(cup, match_id)).text

    assert "This tournament is archived" in page
    assert re.search(r"<button[^>]*disabled[^>]*><span>Save plan", page)
    assert "Pull scores" not in page
    assert snapshot_count(db) == 0  # nothing is written in an archived tournament


# --- snapshots ---------------------------------------------------------------------------------


def test_the_first_view_per_day_keeps_a_snapshot(client, login, db, cup, match_id, clock):
    login(MATE)

    client.get(url(cup, match_id))
    client.get(url(cup, match_id))
    assert snapshot_count(db) == 1

    clock.current += timedelta(days=1)
    client.get(url(cup, match_id))
    assert snapshot_count(db) == 2


def test_viewing_a_finished_match_keeps_no_snapshot(client, login, db, cup):
    fill_scores(db)
    done = add_game(db, cup, status="done", score1=2, score2=0)
    login(MATE)

    assert client.get(url(cup, done)).status_code == 200
    assert snapshot_count(db) == 0


# --- saving the plan ---------------------------------------------------------------------------


def test_a_captain_saves_a_plan_and_sees_it_again(client, login, csrf, db, cup, match_id):
    login(CAPTAIN)
    maps = cup["maps"]

    saved = post_plan(
        client,
        csrf(),
        cup,
        match_id,
        ban_1=str(maps["HD1"]),
        ban_note_1="Their best",
        pick_1=str(maps["NM1"]),
    )

    assert saved.status_code == 303 and saved.headers["location"] == url(cup, match_id)
    assert plan_rows(db) == [("ban", maps["HD1"], 1, "Their best"), ("pick", maps["NM1"], 1, None)]
    page = client.get(saved.headers["location"]).text
    assert "Plan saved." in page
    assert selected(page, "ban_1") == str(maps["HD1"])
    assert selected(page, "pick_1") == str(maps["NM1"])
    assert 'value="Their best"' in page
    assert snapshot_count(db) >= 1


def test_saving_an_unchanged_plan_says_so(client, login, csrf, db, cup, match_id):
    login(CAPTAIN)
    token = csrf()
    fields = {"ban_1": str(cup["maps"]["HD1"])}
    post_plan(client, token, cup, match_id, **fields)
    before = snapshot_count(db)

    again = post_plan(client, token, cup, match_id, **fields)

    assert again.status_code == 303
    assert "The plan is unchanged." in client.get(again.headers["location"]).text
    assert snapshot_count(db) == before


def test_a_refused_plan_shows_the_error_at_the_field_and_keeps_the_input(
    client, login, csrf, db, cup, match_id
):
    login(CAPTAIN)
    maps = cup["maps"]

    refused = post_plan(
        client,
        csrf(),
        cup,
        match_id,
        ban_1=str(maps["NM1"]),
        pick_1=str(maps["NM1"]),
        pick_note_1="Keep me",
    )

    assert refused.status_code == 422
    assert "A banned map cannot be picked." in refused.text
    assert 'aria-invalid="true"' in refused.text
    assert selected(refused.text, "ban_1") == str(maps["NM1"])  # what was typed is still there
    assert 'value="Keep me"' in refused.text
    assert plan_rows(db) == []


def test_a_tiebreaker_a_foreign_id_and_a_long_note_are_refused(
    client, login, csrf, db, cup, match_id
):
    login(CAPTAIN)
    token = csrf()

    for fields in (
        {"ban_1": str(cup["maps"]["TB"])},
        {"ban_1": "999999"},
        {"ban_1": str(cup["maps"]["NM1"]), "ban_note_1": "x" * 301},
    ):
        assert post_plan(client, token, cup, match_id, **fields).status_code == 422, fields
    assert plan_rows(db) == []


def test_a_plan_for_a_match_we_do_not_play_is_a_conflict(client, login, csrf, db, cup):
    other = add_team(db, cup["id"], "Third Team", [(3101, "ThirdA")])
    foreign = db.execute(
        "INSERT INTO matches (tournament_id, stage_id, team1_id, team2_id, status, updated_at) "
        "VALUES (?, ?, ?, ?, 'scheduled', '2026-11-01T00:00:00Z')",
        (cup["id"], cup["stage"], cup["rivals"], other),
    ).lastrowid
    login(CAPTAIN)

    response = post_plan(client, csrf(), cup, foreign, ban_1=str(cup["maps"]["NM1"]))

    assert response.status_code == 409
    assert "only be made for a match our team plays in" in response.text
    assert plan_rows(db) == []


@pytest.mark.parametrize(
    ("who", "status"),
    [(MATE, 403), (GUEST, 403), (STRANGER, 404), (ADMIN_ID, 303)],
)
def test_who_may_save_a_plan(client, login, csrf, db, cup, match_id, who, status):
    login(who)

    response = post_plan(client, csrf(), cup, match_id, ban_1=str(cup["maps"]["HD1"]))

    assert response.status_code == status
    assert len(plan_rows(db)) == (1 if status == 303 else 0)


def test_a_plan_cannot_be_saved_in_an_archived_tournament_or_without_a_token(
    client, login, csrf, db, cup, match_id
):
    login(CAPTAIN)
    token = csrf()
    assert post_plan(client, {}, cup, match_id, ban_1=str(cup["maps"]["HD1"])).status_code == 403

    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    response = post_plan(client, token, cup, match_id, ban_1=str(cup["maps"]["HD1"]))

    assert response.status_code == 409 and "archived" in response.text
    assert plan_rows(db) == []


# --- pickems -----------------------------------------------------------------------------------


def test_pickems_list_the_stage_with_winner_chance_and_score(client, login, db, cup, match_id):
    login(MATE)

    page = client.get(f"/t/{cup['id']}/pickems")

    assert page.status_code == 200
    text = page.text
    assert "Round of 16" in text and 'aria-current="page"' in text
    assert f'href="/t/{cup["id"]}/matches/{match_id}"' in text
    assert re.search(
        r"<td>Alpha Squad</td>\s*<td class=\"is-numeric\"><span class=\"num\">\d+%", text
    )
    assert re.search(r'<span class="num">2-[01]</span>', text)
    assert "Copy as text" in text
    assert 'data-copy="Pickems: Round of 16\nAlpha Squad vs Rival Crew: Alpha Squad' in text


def test_a_pickem_without_scores_names_the_reason(client, login, db, cup):
    add_game(db, cup)
    login(MATE)

    text = client.get(f"/t/{cup['id']}/pickems?stage={cup['stage']}").text

    assert "No prediction: no scores yet for both teams" in text


def test_pickems_for_an_empty_stage_and_a_stageless_tournament(client, login, db, cup):
    login(MATE)
    assert "No matches are listed for this stage." in client.get(f"/t/{cup['id']}/pickems").text

    bare = add_tournament(db, "Bare Cup", acronym="BC")
    add_member(db, bare, MATE, "player")
    assert "has no stages yet" in client.get(f"/t/{bare}/pickems").text


def test_pickems_are_only_for_members(client, login, db, cup):
    login(STRANGER)

    assert client.get(f"/t/{cup['id']}/pickems").status_code == 404


def test_the_navigation_lists_pickems(client, login, cup):
    login(MATE)

    page = client.get(f"/t/{cup['id']}").text

    assert f'href="/t/{cup["id"]}/pickems"' in page


# --- the qualifier seed ------------------------------------------------------------------------


def test_the_overview_and_the_match_page_show_the_estimated_seed_during_the_qualifier(
    client, login, db, cup, match_id
):
    db.execute("UPDATE stages SET is_qualifier = 1 WHERE id = ?", (cup["stage"],))
    login(MATE)
    line = "Estimated seed: 1 of 2 (based on 2 teams with data)"

    assert line in client.get(f"/t/{cup['id']}").text
    assert line in client.get(url(cup, match_id)).text


def test_no_seed_is_shown_outside_a_qualifier(client, login, cup, match_id):
    login(MATE)

    assert "Estimated seed" not in client.get(f"/t/{cup['id']}").text
    assert "Estimated seed" not in client.get(url(cup, match_id)).text


# --- links from other pages --------------------------------------------------------------------


def test_the_schedule_links_to_the_match_page(client, login, db, cup, match_id):
    played = add_game(db, cup, when="2026-11-01T18:00:00Z", status="done", score1=2, score2=1)
    login(MATE)

    page = client.get(f"/t/{cup['id']}/schedule").text

    assert page.count(f'href="/t/{cup["id"]}/matches/{match_id}"') == 1
    assert page.count(f'href="/t/{cup["id"]}/matches/{played}"') == 1


def test_pulling_scores_from_a_match_page_comes_back_to_it(client, login, csrf, db, cup, match_id):
    login(CAPTAIN)
    token = csrf()

    response = client.post(
        f"/t/{cup['id']}/opponents/{cup['rivals']}/capture",
        data={**token, "return_to": f"match-{match_id}"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == url(cup, match_id)


@pytest.mark.parametrize("return_to", ["match-999", "match-abc", "elsewhere", "match-"])
def test_an_unknown_return_target_falls_back_to_the_overview(
    client, login, csrf, cup, match_id, return_to
):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/opponents/{cup['rivals']}/capture",
        data={**csrf(), "return_to": return_to},
        follow_redirects=False,
    )

    assert response.headers["location"] == f"/t/{cup['id']}"


def test_a_match_of_another_tournament_is_not_a_return_target(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    foreign = db.execute(
        "INSERT INTO matches (tournament_id, status, updated_at) "
        "VALUES (?, 'scheduled', '2026-11-01T00:00:00Z')",
        (other,),
    ).lastrowid
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/opponents/{cup['rivals']}/capture",
        data={**csrf(), "return_to": f"match-{foreign}"},
        follow_redirects=False,
    )

    assert response.headers["location"] == f"/t/{cup['id']}"
