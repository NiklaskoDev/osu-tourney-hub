"""The lineup page: the suggestion, copying it, and the captain's saved lineup."""

import pytest

from tests.support import (
    ADMIN_ID,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER_PLAYER, SUB, GUEST, STRANGER = 2001, 2002, 2003, 2005, 2004, 2009


@pytest.fixture
def cup(db):
    """Two players per map. Beatmaps 1001 (NM1), 1002 (FM1) and 1003 (HD1)."""
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER_PLAYER, "player")
    add_member(db, tid, SUB, "player", is_substitute=True)
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid, lineup_size=2)
    maps = {
        "NM1": add_pool_map(db, stage, "NM1"),
        "FM1": add_pool_map(db, stage, "FM1", freemod=True),
        "HD1": add_pool_map(db, stage, "HD1", required_mods="HD"),
    }
    add_score(db, 1001, CAPTAIN, 900_000)
    add_score(db, 1001, PLAYER, 800_000)
    add_score(db, 1001, OTHER_PLAYER, 700_000)
    add_score(db, 1001, SUB, 950_000)
    add_score(db, 1002, PLAYER, 810_000, mods="HD")
    add_score(db, 1003, PLAYER, 600_000, mods="HD")
    return {"id": tid, "stage": stage, "maps": maps}


def entries(db):
    return [
        tuple(row)
        for row in db.execute(
            "SELECT pool_map_id, osu_id, mods FROM lineup_entries ORDER BY pool_map_id, rowid"
        )
    ]


def lineup_rows(db):
    return [
        tuple(row)
        for row in db.execute("SELECT stage_id, match_id, status, updated_by FROM lineups")
    ]


def copy(client, token, cup, **fields):
    return client.post(
        f"/t/{cup['id']}/lineup/copy-suggestion",
        data={**token, "stage": str(cup["stage"]), **fields},
        follow_redirects=False,
    )


def save(client, token, cup, values=None, status="draft", stage=None):
    data = {**token, "stage": str(stage or cup["stage"]), "status": status, **(values or {})}
    return client.post(f"/t/{cup['id']}/lineup", data=data, follow_redirects=False)


def suggestion_of(page):
    """The HTML between the suggestion heading and the saved lineup heading."""
    return page.split('id="suggestion-heading"')[1].split('id="saved-heading"')[0]


def pick(cup, slot, index):
    return f"pick_{cup['maps'][slot]}_{index}"


# --- the suggestion ---


def test_the_suggestion_lists_the_top_players_per_map_without_substitutes(client, login, cup):
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/lineup").text
    suggestion = suggestion_of(page)

    assert suggestion.index("player2001") < suggestion.index("player2002")  # 900k before 800k
    assert "player2005" not in suggestion  # the substitute scored higher but is left out
    assert "player2003" not in suggestion.split("Song FM1")[0]


def test_substitutes_are_included_when_asked_for(client, login, cup):
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/lineup?stage={cup['stage']}&subs=1").text

    assert "player2005" in suggestion_of(page)
    assert 'name="subs" value="1" checked' in page


def test_guests_are_never_suggested(client, login, db, cup):
    add_score(db, 1001, GUEST, 990_000)
    login(PLAYER)

    assert "player2004" not in client.get(f"/t/{cup['id']}/lineup?subs=1").text


def test_a_map_with_too_few_scores_says_so(client, login, cup):
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/lineup").text

    assert "Only 1 of 2 have a score." in page  # FM1 and HD1 have one scorer each


def test_the_freemod_rule_shapes_the_suggestion(client, login, db, cup):
    db.execute(
        "UPDATE tournaments SET freemod_rule = 'two_distinct_mods' WHERE id = ?", (cup["id"],)
    )
    add_score(db, 1002, OTHER_PLAYER, 700_000, mods="HD")
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/lineup").text

    assert "needs two different mods" in page


def test_the_suggestion_respects_the_tournaments_scoring_type(client, login, db, cup):
    db.execute("UPDATE tournaments SET scoring = 'lazer' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/lineup").text

    assert "Nobody has a score" in page and "Lazer standardised" in page


def test_the_lineup_size_comes_from_the_stage_and_is_one_in_a_solo_tournament(
    client, login, db, cup
):
    db.execute("UPDATE stages SET lineup_size = 1 WHERE id = ?", (cup["stage"],))
    login(PLAYER)
    page = client.get(f"/t/{cup['id']}/lineup").text
    assert "The 1 player with the best score" in page
    assert "player2002" not in suggestion_of(page).split("Song FM1")[0]

    db.execute("UPDATE stages SET lineup_size = 3 WHERE id = ?", (cup["stage"],))
    db.execute("UPDATE tournaments SET is_solo = 1 WHERE id = ?", (cup["id"],))
    assert "The 1 player with" in client.get(f"/t/{cup['id']}/lineup").text


def test_empty_states_for_a_missing_stage_or_pool(client, login, db, cup):
    bare = add_tournament(db, "Bare Cup")
    add_member(db, bare, PLAYER, "player")
    empty_stage = add_stage(db, bare, released=False)
    login(PLAYER)

    page = client.get(f"/t/{bare}/lineup?stage={empty_stage}").text
    assert "This stage has no maps yet." in page
    db.execute("DELETE FROM stages WHERE tournament_id = ?", (bare,))
    assert "has no stages yet" in client.get(f"/t/{bare}/lineup").text


def test_the_availability_filter_is_not_offered_yet(client, login, cup):
    login(CAPTAIN)

    assert "only players available" not in client.get(f"/t/{cup['id']}/lineup").text.lower()


# --- copying the suggestion ---


def test_a_captain_copies_the_suggestion_as_a_draft(client, login, csrf, db, cup):
    login(CAPTAIN)

    response = copy(client, csrf(), cup)

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{cup['id']}/lineup?stage={cup['stage']}"
    nm, fm, hd = cup["maps"]["NM1"], cup["maps"]["FM1"], cup["maps"]["HD1"]
    assert entries(db) == [
        (nm, CAPTAIN, ""),
        (nm, PLAYER, ""),
        (fm, PLAYER, "HD"),
        (hd, PLAYER, "HD"),
    ]
    assert lineup_rows(db) == [(cup["stage"], None, "draft", CAPTAIN)]
    page = client.get(response.headers["location"]).text
    assert "Suggested lineup copied" in page and "Draft" in page


def test_copying_with_substitutes_includes_them(client, login, csrf, db, cup):
    login(CAPTAIN)

    copy(client, csrf(), cup, include_substitutes="1")

    assert (cup["maps"]["NM1"], SUB, "") in entries(db)


def test_copying_again_replaces_the_lineup_and_turns_a_final_one_back_into_a_draft(
    client, login, csrf, db, cup
):
    login(CAPTAIN)
    token = csrf()
    copy(client, token, cup)
    save(client, token, cup, {pick(cup, "NM1", 0): str(OTHER_PLAYER)}, status="final")
    assert lineup_rows(db)[0][2] == "final"

    copy(client, token, cup)

    assert lineup_rows(db) == [(cup["stage"], None, "draft", CAPTAIN)]
    assert (cup["maps"]["NM1"], OTHER_PLAYER, "") not in entries(db)
    assert len(entries(db)) == 4


def test_players_guests_and_strangers_cannot_copy(client, login, csrf, db, cup):
    for viewer, status in ((PLAYER, 403), (GUEST, 403), (STRANGER, 404)):
        login(viewer)
        assert copy(client, csrf(), cup).status_code == status
    assert entries(db) == [] and lineup_rows(db) == []


def test_a_temporary_captain_can_copy(client, login, csrf, db, cup):
    db.execute(
        "UPDATE tournament_members SET temp_captain_until = '2099-01-01T00:00:00Z' "
        "WHERE osu_id = ?",
        (PLAYER,),
    )
    login(PLAYER)

    assert copy(client, csrf(), cup).status_code == 303


def test_copying_for_a_stage_of_another_tournament_is_not_found(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup")
    foreign = add_stage(db, other)
    login(CAPTAIN)

    for stage in (str(foreign), "999", "abc", ""):
        response = client.post(
            f"/t/{cup['id']}/lineup/copy-suggestion", data={**csrf(), "stage": stage}
        )
        assert response.status_code == 404, stage
    assert lineup_rows(db) == []


# --- reading and editing the saved lineup ---


def test_a_player_sees_the_saved_lineup_read_only(client, login, csrf, cup):
    login(CAPTAIN)
    copy(client, csrf(), cup)
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/lineup").text

    assert "Draft" in page and "Save lineup" not in page and "Copy suggested lineup" not in page
    assert "Saved lineup for Qualifiers" in page


def test_without_a_saved_lineup_a_player_is_told_a_captain_can_start_one(client, login, cup):
    login(PLAYER)

    assert "No lineup is saved yet." in client.get(f"/t/{cup['id']}/lineup").text


def test_a_captain_edits_and_finalises_the_lineup(client, login, csrf, db, cup):
    login(CAPTAIN)
    token = csrf()
    nm, fm = cup["maps"]["NM1"], cup["maps"]["FM1"]

    response = save(
        client,
        token,
        cup,
        {
            pick(cup, "NM1", 0): str(PLAYER),
            pick(cup, "NM1", 1): str(OTHER_PLAYER),
            pick(cup, "FM1", 0): str(CAPTAIN),
            f"mods_{fm}_0": "hr hd",
            pick(cup, "HD1", 0): str(PLAYER),
            f"mods_{cup['maps']['HD1']}_0": "HR",  # ignored: not a freemod slot
        },
        status="final",
    )

    assert response.status_code == 303
    assert entries(db) == [
        (nm, PLAYER, ""),
        (nm, OTHER_PLAYER, ""),
        (fm, CAPTAIN, "HDHR"),
        (cup["maps"]["HD1"], PLAYER, "HD"),
    ]
    assert lineup_rows(db) == [(cup["stage"], None, "final", CAPTAIN)]
    assert "Final" in client.get(response.headers["location"]).text


def test_saving_twice_keeps_one_lineup_per_stage(client, login, csrf, db, cup):
    login(CAPTAIN)
    token = csrf()

    save(client, token, cup, {pick(cup, "NM1", 0): str(PLAYER)})
    save(client, token, cup, {pick(cup, "NM1", 0): str(OTHER_PLAYER)}, status="final")

    assert len(lineup_rows(db)) == 1
    assert entries(db) == [(cup["maps"]["NM1"], OTHER_PLAYER, "")]


def test_empty_slots_are_skipped_and_the_form_can_clear_a_lineup(client, login, csrf, db, cup):
    login(CAPTAIN)
    token = csrf()
    save(client, token, cup, {pick(cup, "NM1", 0): str(PLAYER)})

    save(client, token, cup, {pick(cup, "NM1", 0): ""})

    assert entries(db) == []


def test_the_saved_lineup_fills_the_form(client, login, csrf, cup):
    login(CAPTAIN)
    copy(client, csrf(), cup)

    page = client.get(f"/t/{cup['id']}/lineup").text

    select = page.split(f'name="{pick(cup, "NM1", 0)}"')[1].split("</select>")[0]
    assert f'<option value="{CAPTAIN}" selected>' in select


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"p0": "2002", "p1": "2002"}, "Choose each player once per map."),
        ({"p0": "2004"}, "Choose a player from the roster."),  # a guest
        ({"p0": "9999"}, "Choose a player from the roster."),
        ({"p0": "abc"}, "Choose a player from the roster."),
        ({"p0": "2002", "mods": "XX"}, "XX is not a mod in osu!."),
        ({"p0": "2002", "mods": "H"}, "is not a list of two-letter mod acronyms"),
    ],
)
def test_invalid_lineups_are_explained_inline_and_the_input_is_kept(
    client, login, csrf, db, cup, values, message
):
    login(CAPTAIN)
    fm = cup["maps"]["FM1"]
    form = {pick(cup, "NM1", 0): values["p0"]}
    if "p1" in values:
        form[pick(cup, "NM1", 1)] = values["p1"]
    if "mods" in values:
        form = {pick(cup, "FM1", 0): values["p0"], f"mods_{fm}_0": values["mods"]}

    response = save(client, csrf(), cup, form)

    assert response.status_code == 422
    assert message in response.text
    assert "field__error" in response.text
    assert lineup_rows(db) == []


def test_a_refused_lineup_keeps_what_was_chosen(client, login, csrf, db, cup):
    login(CAPTAIN)
    form = {pick(cup, "NM1", 0): str(PLAYER), f"mods_{cup['maps']['FM1']}_0": "XX"}

    response = save(client, csrf(), cup, {**form, pick(cup, "FM1", 0): str(PLAYER)})

    assert response.status_code == 422
    select = response.text.split(f'name="{pick(cup, "NM1", 0)}"')[1].split("</select>")[0]
    assert f'<option value="{PLAYER}" selected>' in select
    assert 'value="XX"' in response.text


def test_an_unknown_status_is_refused(client, login, csrf, db, cup):
    login(CAPTAIN)

    response = save(client, csrf(), cup, status="published")

    assert response.status_code == 422 and "Choose draft or final." in response.text
    assert lineup_rows(db) == []


def test_players_guests_and_strangers_cannot_save(client, login, csrf, db, cup):
    for viewer, status in ((PLAYER, 403), (GUEST, 403), (STRANGER, 404)):
        login(viewer)
        assert save(client, csrf(), cup, {pick(cup, "NM1", 0): str(PLAYER)}).status_code == status
    assert lineup_rows(db) == []


def test_saving_for_a_stage_of_another_tournament_is_not_found(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup")
    foreign = add_stage(db, other)
    login(CAPTAIN)

    assert save(client, csrf(), cup, stage=foreign).status_code == 404
    assert lineup_rows(db) == []


def test_slots_beyond_the_lineup_size_are_ignored(client, login, csrf, db, cup):
    login(CAPTAIN)

    save(client, csrf(), cup, {pick(cup, "NM1", 2): str(PLAYER)})

    assert entries(db) == []


def test_an_admin_outside_the_roster_can_save(client, login, csrf, db, cup):
    login(ADMIN_ID)

    assert save(client, csrf(), cup, {pick(cup, "NM1", 0): str(PLAYER)}).status_code == 303
    assert lineup_rows(db)[0][3] == ADMIN_ID


# --- archived ---


def test_an_archived_tournament_shows_the_lineup_but_rejects_changes(client, login, csrf, db, cup):
    login(CAPTAIN)
    token = csrf()
    copy(client, token, cup)
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))

    page = client.get(f"/t/{cup['id']}/lineup")
    copied = copy(client, token, cup)
    saved = save(client, token, cup, {pick(cup, "NM1", 0): str(OTHER_PLAYER)})

    assert page.status_code == 200 and "This tournament is archived" in page.text
    assert (copied.status_code, saved.status_code) == (409, 409)
    assert len(entries(db)) == 4


def test_deleting_a_tournament_removes_its_lineup_and_notes_but_keeps_scores(
    client, login, csrf, db, cup
):
    login(CAPTAIN)
    copy(client, csrf(), cup)
    add_score(db, 1001, PLAYER, 123_456, tournament_id=cup["id"])

    db.execute("DELETE FROM tournaments WHERE id = ?", (cup["id"],))

    assert lineup_rows(db) == [] and entries(db) == []
    row = db.execute("SELECT tournament_id FROM scores WHERE score = 123456").fetchone()
    assert row[0] is None
