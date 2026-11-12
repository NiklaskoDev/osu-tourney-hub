import pytest

from app.domain.models import Stage, Tournament
from app.services.tournament_settings import effective_lineup_size
from tests.support import ADMIN_ID, add_member, add_tournament


@pytest.fixture
def cup(client, login, csrf, db, wybin) -> int:
    """The synthetic team tournament, imported by the admin: Carl and Paul are captains, Pia a
    player, Rex (an opponent) is not a member."""
    wybin.load_defaults()
    login(ADMIN_ID, "AdminAmy")
    client.post("/admin/tournaments/import", data={**csrf(), "link": "synthetic-cup"})
    return db.execute("SELECT id FROM tournaments WHERE wybin_id = 9001").fetchone()[0]


@pytest.fixture
def solo(client, login, csrf, db, wybin) -> int:
    wybin.load_defaults()
    login(ADMIN_ID, "AdminAmy")
    client.post("/admin/tournaments/import", data={**csrf(), "link": "synthetic-solo"})
    return db.execute("SELECT id FROM tournaments WHERE wybin_id = 9002").fetchone()[0]


def post(client, tid, token, **fields):
    return client.post(f"/t/{tid}/settings", data={**token, **fields}, follow_redirects=False)


def valid(**overrides):
    return {"freemod_rule": "none", "scoring": "v2", **overrides}


def stage_ids(db, tid):
    return {
        row["name"]: row["id"]
        for row in db.execute("SELECT id, name FROM stages WHERE tournament_id = ?", (tid,))
    }


def rules(db, tid):
    return tuple(
        db.execute(
            "SELECT freemod_rule, scoring, final_placing, our_team_id "
            "FROM tournaments WHERE id = ?",
            (tid,),
        ).fetchone()
    )


def lineup_sizes(db, tid):
    return {
        row["name"]: row["lineup_size"]
        for row in db.execute(
            "SELECT name, lineup_size FROM stages WHERE tournament_id = ?", (tid,)
        )
    }


# --- who may open it ---


def test_captains_and_temporary_captains_open_the_settings(client, login, cup):
    for captain in (3001, 3003):
        login(captain)
        assert client.get(f"/t/{cup}/settings").status_code == 200


def test_players_guests_and_outsiders_cannot_open_or_save_them(client, login, csrf, db, cup):
    add_member(db, cup, 7001, "guest")
    for viewer, status in ((3002, 403), (7001, 403), (4001, 404)):
        login(viewer)
        assert client.get(f"/t/{cup}/settings").status_code == status
        assert post(client, cup, csrf() if viewer != 4001 else {}, **valid()).status_code in (
            status,
            403,
        )
    assert rules(db, cup)[:2] == ("none", "v2")


def test_saving_needs_the_csrf_token(client, login, cup, db):
    login(3001)

    response = client.post(f"/t/{cup}/settings", data=valid(scoring="lazer"))

    assert response.status_code == 403
    assert rules(db, cup)[1] == "v2"


def test_the_settings_link_is_in_the_navigation_for_captains_only(client, login, cup):
    login(3001)
    assert f'href="/t/{cup}/settings"' in client.get(f"/t/{cup}").text
    login(3002)
    assert f'href="/t/{cup}/settings"' not in client.get(f"/t/{cup}").text


# --- saving ---


def test_a_captain_saves_rules_and_lineup_sizes(client, login, csrf, db, cup):
    ids = stage_ids(db, cup)
    login(3001)

    response = post(
        client,
        cup,
        csrf(),
        **valid(
            freemod_rule="two_distinct_mods",
            scoring="lazer",
            **{f"lineup_size_{ids['Qualifiers']}": "4", f"lineup_size_{ids['Finals']}": " 5 "},
        ),
    )

    assert response.status_code == 303 and response.headers["location"] == f"/t/{cup}/settings"
    assert rules(db, cup)[:2] == ("two_distinct_mods", "lazer")
    assert lineup_sizes(db, cup) == {"Qualifiers": 4, "Round of 16": None, "Finals": 5}
    assert "Settings saved." in client.get(f"/t/{cup}/settings").text


def test_saved_values_are_shown_again(client, login, csrf, db, cup):
    ids = stage_ids(db, cup)
    login(3001)
    post(client, cup, csrf(), **valid(scoring="v1", **{f"lineup_size_{ids['Finals']}": "5"}))

    page = client.get(f"/t/{cup}/settings").text

    assert '<option value="v1" selected>' in page
    assert f'name="lineup_size_{ids["Finals"]}" type="text" value="5"' in page


def test_an_empty_lineup_size_goes_back_to_the_default(client, login, csrf, db, cup):
    ids = stage_ids(db, cup)
    login(3001)
    post(client, cup, csrf(), **valid(**{f"lineup_size_{ids['Finals']}": "5"}))

    post(client, cup, csrf(), **valid(**{f"lineup_size_{ids['Finals']}": ""}))

    assert lineup_sizes(db, cup)["Finals"] is None


def test_the_default_lineup_size_is_the_smallest_team_size(client, login, cup):
    login(3001)

    page = client.get(f"/t/{cup}/settings").text

    assert 'placeholder="3"' in page
    assert "the smallest team size" in page


@pytest.mark.parametrize("size", ["0", "7", "-1", "two", "3.5"])
def test_an_invalid_lineup_size_is_refused_inline_and_nothing_is_saved(
    client, login, csrf, db, cup, size
):
    ids = stage_ids(db, cup)
    field = f"lineup_size_{ids['Finals']}"
    login(3001)

    response = post(client, cup, csrf(), **valid(scoring="lazer", **{field: size}))

    assert response.status_code == 422
    assert "Lineup size must be a whole number from 1 to 6." in response.text  # team size max is 6
    assert f'value="{size}"' in response.text
    assert rules(db, cup)[1] == "v2"  # the valid scoring change was not applied either
    assert lineup_sizes(db, cup)["Finals"] is None


def test_an_unknown_rule_or_scoring_is_refused(client, login, csrf, db, cup):
    login(3001)

    bad_rule = post(client, cup, csrf(), **valid(freemod_rule="anything"))
    bad_scoring = post(client, cup, csrf(), **valid(scoring="v9"))
    missing = post(client, cup, csrf())

    assert bad_rule.status_code == 422 and "Choose one of the listed rules." in bad_rule.text
    assert (
        bad_scoring.status_code == 422
        and "Choose one of the listed scoring types." in bad_scoring.text
    )
    assert missing.status_code == 422
    assert rules(db, cup)[:2] == ("none", "v2")


def test_lineup_sizes_for_stages_of_other_tournaments_are_ignored(client, login, csrf, db, cup):
    other = add_tournament(db, "Other", acronym="O")
    foreign = db.execute(
        "INSERT INTO stages (tournament_id, name, sort_order, is_qualifier) "
        "VALUES (?, 'Foreign', 1, 0)",
        (other,),
    ).lastrowid
    login(3001)

    post(client, cup, csrf(), **valid(**{f"lineup_size_{foreign}": "4", "lineup_size_x": "2"}))

    assert (
        db.execute("SELECT lineup_size FROM stages WHERE id = ?", (foreign,)).fetchone()[0] is None
    )


# --- admin-only settings ---


def test_a_captain_cannot_change_our_team_or_the_final_placing(client, login, csrf, db, cup):
    rival = db.execute("SELECT id FROM teams WHERE name = 'Rival Crew'").fetchone()[0]
    before = rules(db, cup)
    login(3001)

    page = client.get(f"/t/{cup}/settings").text
    response = post(client, cup, csrf(), **valid(our_team=str(rival), final_placing="Champions"))

    assert "Administrator settings" not in page
    assert response.status_code == 303
    assert rules(db, cup) == before


def test_an_admin_sets_the_final_placing(client, login, csrf, db, cup):
    login(ADMIN_ID)

    post(client, cup, csrf(), **valid(final_placing=" Top 8 "))
    assert rules(db, cup)[2] == "Top 8"
    post(client, cup, csrf(), **valid(final_placing=""))

    assert rules(db, cup)[2] is None


def test_a_too_long_final_placing_is_refused(client, login, csrf, db, cup):
    login(ADMIN_ID)

    response = post(client, cup, csrf(), **valid(final_placing="x" * 61))

    assert response.status_code == 422
    assert "Keep the final placing under 60 characters." in response.text
    assert rules(db, cup)[2] is None


def test_an_admin_switches_our_team_and_its_roster_joins_as_players(client, login, csrf, db, cup):
    rival = db.execute("SELECT id FROM teams WHERE name = 'Rival Crew'").fetchone()[0]
    login(ADMIN_ID)

    response = post(client, cup, csrf(), **valid(our_team=str(rival)))

    assert response.status_code == 303
    assert rules(db, cup)[3] == rival
    assert db.execute("SELECT name FROM teams WHERE is_ours = 1").fetchone()[0] == "Rival Crew"
    roles = dict(
        db.execute(
            "SELECT osu_id, role FROM tournament_members "
            "WHERE tournament_id = ? AND source = 'wybin'",
            (cup,),
        ).fetchall()
    )
    assert roles[4001] == roles[4002] == roles[4003] == "player"  # not captains: not the first pick
    assert roles[3001] == "captain"  # the earlier roster stays
    assert db.execute("SELECT username FROM users WHERE osu_id = 4002").fetchone()[0] == "RivalRia"


def test_choosing_our_team_for_the_first_time_hands_out_captain_roles(
    client, login, csrf, db, wybin
):
    wybin.load_defaults()
    for team in wybin.tournaments["synthetic-cup"]["teams"]:
        team["teamMembers"] = [m for m in team["teamMembers"] if m["user"]["userOsu"]["id"] != 1001]
    login(ADMIN_ID, "AdminAmy")
    client.post("/admin/tournaments/import", data={**csrf(), "link": "synthetic-cup"})
    cup = db.execute("SELECT id FROM tournaments").fetchone()[0]
    rival = db.execute("SELECT id FROM teams WHERE name = 'Rival Crew'").fetchone()[0]
    assert rules(db, cup)[3] is None
    assert "Not set" in client.get(f"/t/{cup}/settings").text

    post(client, cup, csrf(), **valid(our_team=str(rival)))

    roles = dict(db.execute("SELECT osu_id, role FROM tournament_members").fetchall())
    assert roles == {4001: "captain", 4002: "player", 4003: "player"}


@pytest.mark.parametrize("team", ["999999", "abc", "-1"])
def test_a_team_from_elsewhere_cannot_become_ours(client, login, csrf, db, cup, team):
    login(ADMIN_ID)
    before = rules(db, cup)

    response = post(client, cup, csrf(), **valid(our_team=team))

    assert response.status_code == 422
    assert "Choose one of the teams in this tournament." in response.text
    assert rules(db, cup) == before


def test_a_team_of_another_tournament_cannot_become_ours(client, login, csrf, db, cup):
    other = add_tournament(db, "Other", acronym="O")
    foreign = db.execute(
        "INSERT INTO teams (tournament_id, name) VALUES (?, 'Foreign')", (other,)
    ).lastrowid
    login(ADMIN_ID)
    before = rules(db, cup)

    response = post(client, cup, csrf(), **valid(our_team=str(foreign)))

    assert response.status_code == 422
    assert rules(db, cup) == before


# --- solo ---


def test_a_solo_tournament_has_no_lineup_size_to_set(client, login, csrf, db, solo):
    login(ADMIN_ID)

    page = client.get(f"/t/{solo}/settings").text
    post(client, solo, csrf(), **valid(**{f"lineup_size_{stage_ids(db, solo)['Qualifiers']}": "3"}))

    assert "Every map is played by one player in a solo tournament." in page
    assert lineup_sizes(db, solo) == {"Qualifiers": None}


# --- archived ---


def test_an_archived_tournament_rejects_saves_with_409_and_shows_disabled_controls(
    client, login, csrf, db, cup
):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup,))
    login(3001)

    page = client.get(f"/t/{cup}/settings").text
    response = post(client, cup, csrf(), **valid(scoring="lazer"))

    assert "This tournament is archived" in page
    assert page.count("disabled") >= 4
    assert response.status_code == 409
    assert "This tournament is archived" in response.text
    assert rules(db, cup)[1] == "v2"


# --- effective lineup size ---


def stage(lineup_size=None) -> Stage:
    return Stage(1, 1, None, "S", 1, None, None, None, None, None, False, lineup_size, False)


def tournament(**overrides) -> Tournament:
    return Tournament(1, "T", None, "active", **overrides)


@pytest.mark.parametrize(
    ("stage_size", "kwargs", "expected"),
    [
        (4, {"team_size_min": 3}, 4),  # the stage's own setting wins
        (None, {"team_size_min": 3}, 3),  # else the smallest team
        (None, {}, 1),  # else one
        (4, {"is_solo": True, "team_size_min": 1}, 1),  # solo is always one
        (None, {"is_solo": True}, 1),
    ],
)
def test_effective_lineup_size(stage_size, kwargs, expected):
    assert effective_lineup_size(stage(stage_size), tournament(**kwargs)) == expected
