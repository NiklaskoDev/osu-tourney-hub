"""The pool sheet page: what it shows, and adding, removing and annotating scores."""

import pytest

from tests.support import (
    ADMIN_ID,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER_PLAYER, GUEST, STRANGER = 2001, 2002, 2003, 2004, 2009


@pytest.fixture
def cup(db):
    """Spring Cup (osu!, ScoreV2): a captain, two players and a guest; one stage with a nomod,
    a hidden, a freemod and a tiebreaker map (beatmaps 1001 to 1004)."""
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER_PLAYER, "player")
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid)
    maps = {
        "NM1": add_pool_map(db, stage, "NM1"),
        "HD1": add_pool_map(db, stage, "HD1", required_mods="HD"),
        "FM1": add_pool_map(db, stage, "FM1", freemod=True),
        "TB": add_pool_map(db, stage, "TB", tiebreaker=True),
    }
    return {"id": tid, "stage": stage, "maps": maps}


def post_score(client, token, cup, slot="NM1", **fields):
    data = {
        **token,
        "pool_map_id": str(cup["maps"][slot]),
        "score": "800000",
        "accuracy": "97.5",
        **fields,
    }
    return client.post(f"/t/{cup['id']}/scores", data=data, follow_redirects=False)


def scores(db):
    return db.execute(
        "SELECT beatmap_id, osu_id, mods, score, scoring, accuracy, max_combo, misses, source, "
        "source_url, entered_by, tournament_id FROM scores ORDER BY id"
    ).fetchall()


# --- reading the sheet ---


def test_the_sheet_shows_best_scores_accuracy_and_the_team_average(client, login, db, cup):
    add_score(db, 1001, PLAYER, 800_000, accuracy=96.0)
    add_score(db, 1001, PLAYER, 900_000, accuracy=98.0)
    add_score(db, 1001, OTHER_PLAYER, 600_000, accuracy=94.0)
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "Mappool sheet" in page
    assert "900,000" in page and "98.00%" in page
    assert "800,000" not in page.split("Scores and notes per map")[0]  # only the best is shown
    assert "800,000" in page.split("Scores and notes per map")[1]  # but all are listed below
    assert "750,000" in page and "2 players" in page  # team average of 900 000 and 600 000
    assert "ScoreV2" in page


def test_the_sheet_has_a_column_per_player_but_none_for_guests(client, login, cup):
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text

    for name in ("player2001", "player2002", "player2003"):
        assert name in page.split("Scores and notes per map")[0]
    assert "player2004" not in page


def test_map_details_and_footer_averages_are_shown(client, login, db, cup):
    add_score(db, 1001, PLAYER, 800_000, accuracy=96.0)
    add_score(db, 1002, PLAYER, 700_000, accuracy=94.0, mods="HD")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "Synth Artist - Song NM1 [Insane]" in page
    assert "6.25" in page and "3:20" in page
    assert "Average: All maps" in page and "Average: NM" in page and "Average: HD" in page
    assert "750,000" in page and "2/4 maps" in page  # personal average over maps with a score


def test_a_freemod_cell_shows_the_mods_of_the_best_score(client, login, db, cup):
    add_score(db, 1003, PLAYER, 900_000, mods="HDHR")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "HDHR" in page.split("Scores and notes per map")[0]


def test_scores_older_than_six_months_are_marked_old_and_still_count(client, login, db, cup):
    add_score(db, 1001, PLAYER, 900_000, achieved_at="2025-01-01T00:00:00Z")
    add_score(db, 1002, PLAYER, 600_000, mods="HD", achieved_at="2026-11-01T00:00:00Z")
    login(PLAYER)

    sheet = client.get(f"/t/{cup['id']}/pool").text.split("Scores and notes per map")[0]

    assert sheet.count('class="old-flag"') == 1
    assert "900,000" in sheet and "750,000" in sheet  # the old score is in the average


def test_scores_of_another_scoring_type_are_listed_but_do_not_count(client, login, db, cup):
    add_score(db, 1001, PLAYER, 990_000, scoring="v1")
    add_score(db, 1001, PLAYER, 800_000, scoring="v2")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text
    sheet, details = page.split("Scores and notes per map")

    assert "800,000" in sheet and "990,000" not in sheet
    assert "990,000" in details
    assert "Not counted: ScoreV1 score" in details


def test_a_lazer_score_counts_on_a_v2_tournament_and_is_marked(client, login, db, cup):
    add_score(db, 1001, PLAYER, 990_000, scoring="lazer", source="api")
    add_score(db, 1001, PLAYER, 800_000, scoring="v2")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text
    sheet, details = page.split("Scores and notes per map")

    assert "990,000" in sheet and "800,000" not in sheet  # the better score wins the cell
    assert ">lazer</span>" in sheet
    assert 'title="Lazer standardised score from osu!, not ScoreV2"' in sheet
    assert "Best (lazer score)" in details
    assert "<td>Counts</td>" in details  # the real ScoreV2 score is not marked
    assert "close to ScoreV2 but not the same" in page


def test_a_real_v2_score_is_not_marked_lazer(client, login, db, cup):
    add_score(db, 1001, PLAYER, 800_000, scoring="v2")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert ">lazer</span>" not in page and "lazer score" not in page


def test_a_lazer_score_does_not_count_on_a_v1_tournament(client, login, db, cup):
    db.execute("UPDATE tournaments SET scoring = 'v1' WHERE id = ?", (cup["id"],))
    add_score(db, 1001, PLAYER, 990_000, scoring="lazer")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text
    sheet, details = page.split("Scores and notes per map")

    assert "990,000" not in sheet and ">lazer</span>" not in page
    assert "Not counted: Lazer standardised score" in details
    assert "close to ScoreV2" not in page  # the hint is for ScoreV2 tournaments only


def test_a_tournament_using_lazer_counts_only_lazer_scores(client, login, db, cup):
    db.execute("UPDATE tournaments SET scoring = 'lazer' WHERE id = ?", (cup["id"],))
    add_score(db, 1001, PLAYER, 990_000, scoring="lazer")
    add_score(db, 1001, PLAYER, 800_000, scoring="v2")
    login(PLAYER)

    sheet = client.get(f"/t/{cup['id']}/pool").text.split("Scores and notes per map")[0]

    assert "990,000" in sheet and "800,000" not in sheet
    assert "Lazer standardised" in client.get(f"/t/{cup['id']}/pool").text


def test_a_score_with_the_wrong_mods_is_listed_but_does_not_count(client, login, db, cup):
    add_score(db, 1001, PLAYER, 990_000, mods="HR")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text
    sheet, details = page.split("Scores and notes per map")

    assert "990,000" not in sheet
    assert "Mods do not match the map" in details


def test_a_stage_without_a_published_pool_explains_itself(client, login, db, cup):
    later = add_stage(db, cup["id"], "Finals", sort_order=2, released=False)
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool?stage={later}").text

    assert "This stage has no maps yet." in page
    db.execute("UPDATE tournaments SET source = 'wybin' WHERE id = ?", (cup["id"],))
    again = client.get(f"/t/{cup['id']}/pool?stage={later}").text
    assert "not published on wybin yet" in again


def test_a_tournament_without_stages_has_an_empty_state(client, login, db):
    tid = add_tournament(db, "Bare Cup")
    add_member(db, tid, PLAYER, "player")
    login(PLAYER)

    assert "has no stages yet" in client.get(f"/t/{tid}/pool").text


def test_a_roster_without_players_has_an_empty_state(client, login, db):
    tid = add_tournament(db, "Quiet Cup")
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid)
    add_pool_map(db, stage, "NM1")
    login(GUEST)

    assert "no players on its roster yet" in client.get(f"/t/{tid}/pool").text


def test_stage_tabs_pick_the_stage_and_ignore_a_foreign_or_broken_stage_id(client, login, db, cup):
    other = add_tournament(db, "Other Cup")
    foreign = add_stage(db, other, "Foreign stage")
    final = add_stage(db, cup["id"], "Finals", sort_order=2)
    add_pool_map(db, final, "NM1", beatmap_id=2001)
    login(PLAYER)

    first = client.get(f"/t/{cup['id']}/pool").text
    second = client.get(f"/t/{cup['id']}/pool?stage={final}").text

    assert "Synth Artist - Song HD1" in first and "Song HD1" not in second
    for broken in (str(foreign), "abc", "-1", "9" * 30):
        page = client.get(f"/t/{cup['id']}/pool?stage={broken}").text
        assert "Song HD1" in page and "Foreign stage" not in page


def test_guests_can_read_the_sheet_but_get_no_forms(client, login, cup):
    login(GUEST)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "Mappool sheet" in page
    assert "Add score" not in page and "Add a note" not in page


# --- scores from other tournaments ---


@pytest.fixture
def autumn(db, cup):
    """Autumn Open reuses beatmap 1001 and has the same two players."""
    tid = add_tournament(db, "Autumn Open", acronym="AO")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    stage = add_stage(db, tid)
    add_pool_map(db, stage, "NM1", beatmap_id=1001)
    return tid


def test_a_score_entered_in_one_tournament_shows_in_another_with_the_same_beatmap(
    client, login, db, cup, autumn
):
    add_score(db, 1001, PLAYER, 830_000, tournament_id=cup["id"], entered_by=PLAYER)
    login(PLAYER)

    page = client.get(f"/t/{autumn}/pool").text

    assert "830,000" in page and "from Spring Cup" in page


def test_the_same_beatmap_with_other_mods_does_not_carry_over(client, login, db, cup, autumn):
    add_score(db, 1001, PLAYER, 830_000, mods="HR", tournament_id=cup["id"])
    login(PLAYER)

    sheet = client.get(f"/t/{autumn}/pool").text.split("Scores and notes per map")[0]

    assert "830,000" not in sheet


def test_the_origin_hint_does_not_name_a_tournament_the_viewer_cannot_see(
    client, login, db, cup, autumn
):
    secret = add_tournament(db, "Secret Cup", status="hidden")
    add_member(db, secret, PLAYER, "player")
    add_score(db, 1001, PLAYER, 830_000, tournament_id=secret, entered_by=PLAYER)
    login(CAPTAIN)

    page = client.get(f"/t/{autumn}/pool").text

    assert "830,000" in page
    assert "from another tournament" in page and "Secret Cup" not in page


def test_a_score_from_a_deleted_tournament_stays_and_is_called_another_tournament(
    client, login, db, cup, autumn
):
    add_score(db, 1001, PLAYER, 830_000, tournament_id=cup["id"])
    db.execute("DELETE FROM tournaments WHERE id = ?", (cup["id"],))
    login(PLAYER)

    page = client.get(f"/t/{autumn}/pool").text

    assert "830,000" in page and "from another tournament" in page


# --- adding scores ---


def test_a_player_adds_their_own_score(client, login, csrf, db, cup, clock):
    login(PLAYER)

    response = post_score(
        client, csrf(), cup, score="812,345", accuracy="97,85", misses="3", max_combo="1200"
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{cup['id']}/pool?stage={cup['stage']}"
    assert [tuple(row) for row in scores(db)] == [
        (1001, PLAYER, "", 812_345, "v2", 97.85, 1200, 3, "manual", None, PLAYER, cup["id"])
    ]
    page = client.get(response.headers["location"]).text
    assert "Score saved." in page and "812,345" in page


def test_the_score_is_saved_in_the_tournaments_scoring_type(client, login, csrf, db, cup):
    db.execute("UPDATE tournaments SET scoring = 'lazer' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    post_score(client, csrf(), cup)

    assert scores(db)[0]["scoring"] == "lazer"


def test_freemod_and_tiebreaker_scores_take_the_ticked_mods(client, login, csrf, db, cup):
    login(PLAYER)
    token = csrf()

    client.post(
        f"/t/{cup['id']}/scores",
        data={
            **token,
            "pool_map_id": str(cup["maps"]["FM1"]),
            "score": "700000",
            "accuracy": "95",
            "mods": ["HR", "HD"],
        },
    )
    post_score(client, token, cup, slot="TB", mods="DT")

    assert [row["mods"] for row in scores(db)] == ["HDHR", "DT"]


def test_other_maps_are_played_with_their_own_mods_whatever_the_form_says(
    client, login, csrf, db, cup
):
    login(PLAYER)
    token = csrf()

    post_score(client, token, cup, slot="NM1", mods="HR")
    post_score(client, token, cup, slot="HD1", mods="HR")

    assert [row["mods"] for row in scores(db)] == ["", "HD"]


def test_a_pasted_link_is_rebuilt_from_its_id(client, login, csrf, db, cup):
    login(PLAYER)
    token = csrf()

    post_score(client, token, cup, link="https://evil.example/scores/osu/4242?x=<script>")
    post_score(client, token, cup, link="osu.ppy.sh/community/matches/777")

    assert [row["source_url"] for row in scores(db)] == [
        "https://osu.ppy.sh/scores/4242",
        "https://osu.ppy.sh/community/matches/777",
    ]
    page = client.get(f"/t/{cup['id']}/pool").text
    assert 'href="https://osu.ppy.sh/scores/4242"' in page and "evil.example" not in page


def test_a_captain_adds_a_score_for_any_player_and_it_is_recorded_who_typed_it(
    client, login, csrf, db, cup
):
    login(CAPTAIN)

    response = post_score(client, csrf(), cup, osu_id=str(PLAYER))

    assert response.status_code == 303
    assert (scores(db)[0]["osu_id"], scores(db)[0]["entered_by"]) == (PLAYER, CAPTAIN)


def test_an_admin_outside_the_roster_can_add_scores_for_a_player(client, login, csrf, db, cup):
    login(ADMIN_ID)

    assert post_score(client, csrf(), cup, osu_id=str(PLAYER)).status_code == 303
    assert scores(db)[0]["osu_id"] == PLAYER


def test_a_player_cannot_add_a_score_for_someone_else(client, login, csrf, db, cup):
    login(PLAYER)

    response = post_score(client, csrf(), cup, osu_id=str(OTHER_PLAYER))

    assert response.status_code == 403
    assert scores(db) == []


def test_guests_and_strangers_cannot_add_scores(client, login, csrf, db, cup):
    login(GUEST)
    assert post_score(client, csrf(), cup).status_code == 403
    login(STRANGER)
    assert post_score(client, csrf(), cup).status_code == 404
    assert scores(db) == []


def test_a_captain_cannot_enter_a_score_for_a_guest_or_a_non_member(client, login, csrf, db, cup):
    login(CAPTAIN)

    for target in (str(GUEST), str(STRANGER), "abc"):
        response = post_score(client, csrf(), cup, osu_id=target)
        assert response.status_code == 422, target
        assert "Choose a player from the roster." in response.text
    assert scores(db) == []


def test_a_map_of_another_tournament_is_not_found(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup")
    foreign = add_pool_map(db, add_stage(db, other), "NM1")
    login(PLAYER)

    for map_id in (str(foreign), "999999", "abc"):
        response = client.post(
            f"/t/{cup['id']}/scores",
            data={**csrf(), "pool_map_id": map_id, "score": "1", "accuracy": "1"},
        )
        assert response.status_code == 404, map_id
    assert scores(db) == []


def test_a_placeholder_map_cannot_get_scores(client, login, csrf, db, cup):
    db.execute("UPDATE pool_maps SET beatmap_id = NULL WHERE id = ?", (cup["maps"]["NM1"],))
    login(PLAYER)

    response = post_score(client, csrf(), cup)

    assert response.status_code == 422
    assert "no osu! beatmap" in response.text
    assert scores(db) == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"score": ""}, "Enter the score as a whole number from 0 to 2,000,000,000."),
        ({"score": "-5"}, "Enter the score as a whole number from 0 to 2,000,000,000."),
        ({"score": "12.5"}, "Enter the score as a whole number from 0 to 2,000,000,000."),
        ({"score": "2000000001"}, "Enter the score as a whole number from 0 to 2,000,000,000."),
        ({"score": "9" * 30}, "Enter the score as a whole number from 0 to 2,000,000,000."),
        ({"accuracy": "101"}, "Enter the accuracy as a number from 0 to 100"),
        ({"accuracy": "100.01"}, "Enter the accuracy as a number from 0 to 100"),
        ({"accuracy": "-1"}, "Enter the accuracy as a number from 0 to 100"),
        ({"accuracy": "abc"}, "Enter the accuracy as a number from 0 to 100"),
        ({"accuracy": "nan"}, "Enter the accuracy as a number from 0 to 100"),
        ({"accuracy": ""}, "Enter the accuracy as a number from 0 to 100"),
        ({"misses": "-1"}, "Misses must be a whole number"),
        ({"misses": "two"}, "Misses must be a whole number"),
        ({"max_combo": "99999999"}, "Combo must be a whole number"),
        ({"link": "https://example.com/page"}, "Paste an osu! score or multiplayer match link."),
        ({"link": "12345"}, "Paste an osu! score or multiplayer match link."),
    ],
)
def test_invalid_input_is_explained_next_to_the_field_and_kept(
    client, login, csrf, db, cup, fields, message
):
    login(PLAYER)

    response = post_score(client, csrf(), cup, **fields)

    assert response.status_code == 422
    assert message in response.text
    assert "field__error" in response.text and 'aria-invalid="true"' in response.text
    assert "data-autoopen" in response.text  # the dialog opens again with the message in it
    for name, value in fields.items():
        if value and name != "accuracy" and len(value) < 20:
            assert f'value="{value}"' in response.text
    assert scores(db) == []


def test_the_boundaries_of_score_and_accuracy_are_accepted(client, login, csrf, db, cup):
    login(PLAYER)
    token = csrf()

    for score_text, accuracy in (("0", "0"), ("2000000000", "100"), ("1", "99.9999")):
        assert (
            post_score(client, token, cup, score=score_text, accuracy=accuracy).status_code == 303
        )

    assert [row["score"] for row in scores(db)] == [0, 2_000_000_000, 1]


def test_a_mod_the_mode_does_not_have_is_refused_inline_and_the_other_input_is_kept(
    client, login, csrf, db, cup
):
    db.execute("UPDATE tournaments SET gamemode = 3 WHERE id = ?", (cup["id"],))
    login(PLAYER)

    response = client.post(
        f"/t/{cup['id']}/scores",
        data={
            **csrf(),
            "pool_map_id": str(cup["maps"]["FM1"]),
            "score": "712345",
            "accuracy": "96.5",
            "mods": ["HD", "HR"],
        },
    )

    assert response.status_code == 422
    assert "HR is not a mod in osu!mania." in response.text
    assert 'value="712345"' in response.text and 'value="96.5"' in response.text
    assert "checked" in response.text.split('name="mods" value="HD"')[1].split(">")[0]
    assert scores(db) == []


def test_the_mod_list_depends_on_the_game_mode(client, login, db, cup):
    login(PLAYER)
    standard = client.get(f"/t/{cup['id']}/pool").text
    db.execute("UPDATE tournaments SET gamemode = 3 WHERE id = ?", (cup["id"],))
    mania = client.get(f"/t/{cup['id']}/pool").text

    assert 'name="mods" value="HR"' in standard
    assert 'name="mods" value="HR"' not in mania and 'name="mods" value="MR"' in mania


def test_a_player_form_has_no_player_picker_but_a_captain_form_does(client, login, cup):
    login(PLAYER)
    assert 'name="osu_id"' not in client.get(f"/t/{cup['id']}/pool").text
    login(CAPTAIN)
    assert 'name="osu_id"' in client.get(f"/t/{cup['id']}/pool").text


# --- removing scores ---


def test_a_player_removes_their_own_hand_entered_score(client, login, csrf, db, cup):
    score_id = add_score(db, 1001, PLAYER, 800_000, entered_by=PLAYER, tournament_id=cup["id"])
    login(PLAYER)

    response = client.post(
        f"/t/{cup['id']}/scores/{score_id}/delete",
        data={**csrf(), "stage": str(cup["stage"])},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{cup['id']}/pool?stage={cup['stage']}"
    assert scores(db) == []


def test_a_player_cannot_remove_someone_elses_score(client, login, csrf, db, cup):
    score_id = add_score(db, 1001, OTHER_PLAYER, 800_000, tournament_id=cup["id"])
    login(PLAYER)

    response = client.post(f"/t/{cup['id']}/scores/{score_id}/delete", data=csrf())

    assert response.status_code == 403
    assert len(scores(db)) == 1


def test_a_captain_removes_a_members_score_entered_in_this_tournament(client, login, csrf, db, cup):
    score_id = add_score(db, 1001, PLAYER, 800_000, tournament_id=cup["id"])
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/scores/{score_id}/delete", data=csrf(), follow_redirects=False
    )

    assert response.status_code == 303 and scores(db) == []


def test_a_captain_cannot_remove_a_score_that_was_entered_in_another_tournament(
    client, login, csrf, db, cup
):
    elsewhere = add_tournament(db, "Elsewhere Cup")
    score_id = add_score(db, 1001, PLAYER, 800_000, tournament_id=elsewhere)
    login(CAPTAIN)

    response = client.post(f"/t/{cup['id']}/scores/{score_id}/delete", data=csrf())

    assert response.status_code == 403
    assert len(scores(db)) == 1


def test_captured_scores_cannot_be_removed(client, login, csrf, db, cup):
    score_id = add_score(db, 1001, PLAYER, 800_000, source="api")
    login(PLAYER)

    response = client.post(f"/t/{cup['id']}/scores/{score_id}/delete", data=csrf())

    assert response.status_code == 409
    assert "Only scores added by hand can be removed." in response.text
    assert len(scores(db)) == 1


def test_removing_an_unknown_score_is_not_found(client, login, csrf, cup):
    login(PLAYER)

    assert client.post(f"/t/{cup['id']}/scores/999/delete", data=csrf()).status_code == 404
    assert client.post(f"/t/{cup['id']}/scores/abc/delete", data=csrf()).status_code == 404


def test_the_remove_button_shows_only_for_scores_the_viewer_may_remove(client, login, db, cup):
    add_score(db, 1001, PLAYER, 800_000, tournament_id=cup["id"])
    add_score(db, 1001, OTHER_PLAYER, 700_000, tournament_id=cup["id"])
    add_score(db, 1002, PLAYER, 600_000, mods="HD", source="api")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert page.count("Remove score") == 1  # confirm button of the one dialog
    login(CAPTAIN)
    assert client.get(f"/t/{cup['id']}/pool").text.count("Remove score") == 2


# --- notes ---


def post_note(client, token, cup, body="Watch the stream at 1:20", visibility="team", slot="NM1"):
    return client.post(
        f"/t/{cup['id']}/notes",
        data={
            **token,
            "pool_map_id": str(cup["maps"][slot]),
            "body": body,
            "visibility": visibility,
        },
        follow_redirects=False,
    )


def test_a_team_note_is_visible_to_everyone_in_the_tournament(client, login, csrf, cup):
    login(PLAYER)
    assert post_note(client, csrf(), cup).status_code == 303

    for viewer in (OTHER_PLAYER, CAPTAIN, GUEST):
        login(viewer)
        page = client.get(f"/t/{cup['id']}/pool").text
        assert "Watch the stream at 1:20" in page and "Team" in page


def test_a_private_note_is_only_visible_to_its_author(client, login, csrf, cup):
    login(PLAYER)
    post_note(client, csrf(), cup, body="My secret plan", visibility="private")

    assert "My secret plan" in client.get(f"/t/{cup['id']}/pool").text
    for viewer in (OTHER_PLAYER, CAPTAIN, GUEST, ADMIN_ID):
        login(viewer)
        assert "My secret plan" not in client.get(f"/t/{cup['id']}/pool").text


def test_an_author_removes_their_own_note_but_nobody_else_can(client, login, csrf, db, cup):
    login(PLAYER)
    post_note(client, csrf(), cup)
    note_id = db.execute("SELECT id FROM notes").fetchone()[0]

    login(CAPTAIN)
    denied = client.post(f"/t/{cup['id']}/notes/{note_id}/delete", data=csrf())
    login(PLAYER)
    removed = client.post(
        f"/t/{cup['id']}/notes/{note_id}/delete", data=csrf(), follow_redirects=False
    )

    assert denied.status_code == 403
    assert removed.status_code == 303
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_a_note_of_another_tournament_cannot_be_removed_here(client, login, csrf, db, cup, autumn):
    note_id = db.execute(
        "INSERT INTO notes (tournament_id, pool_map_id, author_osu_id, visibility, body, "
        "updated_at) VALUES (?, ?, ?, 'team', 'x', '2026-11-01T00:00:00Z')",
        (
            autumn,
            db.execute("SELECT id FROM pool_maps WHERE stage_id != ?", (cup["stage"],)).fetchone()[
                0
            ],
            PLAYER,
        ),
    ).lastrowid
    login(PLAYER)

    assert client.post(f"/t/{cup['id']}/notes/{note_id}/delete", data=csrf()).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_guests_cannot_write_notes(client, login, csrf, db, cup):
    login(GUEST)

    assert post_note(client, csrf(), cup).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("body", "visibility", "message"),
    [
        ("", "team", "Write a note of 1 to 2000 characters."),
        ("   ", "team", "Write a note of 1 to 2000 characters."),
        ("x" * 2001, "team", "Write a note of 1 to 2000 characters."),
        ("A fine note", "everyone", "Choose who can read the note."),
    ],
)
def test_an_invalid_note_is_explained_and_the_text_kept(
    client, login, csrf, db, cup, body, visibility, message
):
    login(PLAYER)

    response = post_note(client, csrf(), cup, body=body, visibility=visibility)

    assert response.status_code == 422
    assert message in response.text
    if body.strip() and len(body) < 100:
        assert body in response.text
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_a_note_of_exactly_2000_characters_is_accepted(client, login, csrf, db, cup):
    login(PLAYER)

    assert post_note(client, csrf(), cup, body="x" * 2000).status_code == 303


def test_note_text_is_escaped(client, login, csrf, cup):
    login(PLAYER)
    post_note(client, csrf(), cup, body="<script>alert(1)</script>")

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# --- archived tournaments ---


def test_an_archived_tournament_shows_the_sheet_but_rejects_every_write(
    client, login, csrf, db, cup
):
    score_id = add_score(db, 1001, PLAYER, 800_000, tournament_id=cup["id"])
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)
    token = csrf()

    page = client.get(f"/t/{cup['id']}/pool")
    added = post_score(client, token, cup)
    removed = client.post(f"/t/{cup['id']}/scores/{score_id}/delete", data=token)
    noted = post_note(client, token, cup)

    assert page.status_code == 200 and "This tournament is archived" in page.text
    assert (added.status_code, removed.status_code, noted.status_code) == (409, 409, 409)
    assert len(scores(db)) == 1
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_a_guest_reads_team_notes_but_not_private_ones_and_cannot_write(
    client, login, csrf, db, cup
):
    login(PLAYER)
    token = csrf()
    post_note(client, token, cup, body="Team plan for NM1", visibility="team")
    post_note(client, token, cup, body="Private thought", visibility="private")
    login(GUEST)

    page = client.get(f"/t/{cup['id']}/pool").text
    refused = post_note(client, csrf(), cup, body="Guest note")

    assert "Team plan for NM1" in page
    assert "Private thought" not in page
    assert refused.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
