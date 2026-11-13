"""The review page, event corrections, reading a lobby from the page and the opponent page."""

import pytest

from app.clients.osu import OsuClient
from app.clients.ratelimit import Priority
from app.repos import match_events as events_repo
from app.repos import matches as matches_repo
from app.repos import tournaments as tournaments_repo
from app.services import matchup
from tests.matchup_support import CAPTAIN, MATE, NM1, RIVAL_A, add_game, build_cup, fill_scores
from tests.support import ADMIN_ID, add_match, add_team, add_user

LOBBY = 555


@pytest.fixture
def cup(db):
    return build_cup(db)


@pytest.fixture
def match_id(db, cup):
    fill_scores(db)
    return add_game(db, cup, when="2026-11-14T18:00:00Z")


def review_url(cup, match, tail=""):
    return f"/t/{cup['id']}/matches/{match}/review{tail}"


def add_event(db, match_id, seq, kind, side, map_id, winner=None, source="mp"):
    events_repo.save_manual(
        db,
        match_id,
        events_repo.MatchEvent(seq, kind, side, map_id, None, winner, None, None, source),
    )


def finish(db, cup, match_id, clock):
    """Save the pre-match prediction, then play the match: our team wins NM1."""
    tournament = tournaments_repo.get(db, cup["id"])
    view = matchup.build_matchup(db, tournament, matches_repo.get(db, match_id))
    matchup.save_snapshot(db, view, clock.now())
    db.execute("UPDATE matches SET status='done', score1=2, score2=0 WHERE id=?", (match_id,))
    add_event(db, match_id, 100, "pick", 1, cup["maps"]["NM1"], winner=1)


def other_tournament(db):
    db.execute(
        "INSERT INTO tournaments (name, acronym, gamemode, is_solo, status, source, created_at) "
        "VALUES ('Other', 'OT', 0, 0, 'active', 'manual', '2026-01-01T00:00:00Z')"
    )
    return db.execute("SELECT MAX(id) FROM tournaments").fetchone()[0]


def test_a_member_sees_what_was_played_next_to_the_prediction(
    client, login, db, cup, match_id, clock
):
    finish(db, cup, match_id, clock)
    login(MATE)

    page = client.get(review_url(cup, match_id))

    assert page.status_code == 200
    text = page.text
    assert "Review: Alpha Squad vs Rival Crew" in text
    assert "What was played" in text
    assert "Predicted winner" in text and "Alpha Squad" in text
    assert 'result--win">Hit' in text
    assert "Map winners predicted" in text
    assert "Correct events" not in text  # a player cannot change events
    assert "Read lobby again" not in text


def test_a_match_without_events_explains_where_they_come_from(client, login, cup, match_id):
    login(MATE)

    text = client.get(review_url(cup, match_id)).text

    assert "No protects, bans or picks are recorded for this match yet." in text
    assert "no multiplayer link yet" in text
    assert "What was played" not in text


def test_events_without_a_saved_prediction_say_so(client, login, db, cup, match_id):
    add_event(db, match_id, 100, "pick", 1, cup["maps"]["NM1"], winner=1)
    login(MATE)

    text = client.get(review_url(cup, match_id)).text

    assert "No prediction to compare with" in text
    assert "What was played" in text


def test_a_captain_gets_the_correction_form_and_the_lobby_button(client, login, db, cup, match_id):
    add_event(db, match_id, 100, "pick", None, cup["maps"]["NM1"])
    db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (LOBBY, match_id))
    login(CAPTAIN)

    text = client.get(review_url(cup, match_id)).text

    assert "Correct events" in text and 'name="side_100"' in text and 'name="new_kind"' in text
    assert "Read lobby again" in text


def test_a_captain_names_the_first_pick_and_the_others_follow(
    client, login, csrf, db, cup, match_id
):
    maps = cup["maps"]
    add_event(db, match_id, 100, "pick", None, maps["NM1"])
    add_event(db, match_id, 101, "pick", None, maps["NM2"])
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/matches/{match_id}/events",
        data={
            **csrf(),
            "side_100": "2",
            "map_100": str(maps["NM1"]),
            "winner_100": "",
            "side_101": "",
            "map_101": str(maps["NM2"]),
            "winner_101": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Events saved." in response.text
    rows = events_repo.list_for_match(db, match_id)
    assert [(e.seq, e.team_side, e.source) for e in rows] == [(100, 2, "manual"), (101, 1, "mp")]


def test_a_refused_correction_keeps_the_input_and_names_the_field(
    client, login, csrf, db, cup, match_id
):
    add_event(db, match_id, 100, "pick", None, cup["maps"]["NM1"])
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/matches/{match_id}/events",
        data={**csrf(), "new_kind": "ban", "new_map": str(cup["maps"]["NM2"])},
    )

    assert response.status_code == 422
    assert "Choose which team made it." in response.text
    assert 'aria-invalid="true"' in response.text
    assert f'<option value="{cup["maps"]["NM2"]}" selected' in response.text
    assert len(events_repo.list_for_match(db, match_id)) == 1


def test_saving_an_unchanged_form_says_nothing_changed(client, login, csrf, cup, match_id):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/matches/{match_id}/events", data=csrf(), follow_redirects=True
    )

    assert "Nothing was changed." in response.text


def test_an_archived_tournament_refuses_corrections_and_hides_the_form(
    client, login, csrf, db, cup, match_id
):
    add_event(db, match_id, 100, "pick", None, cup["maps"]["NM1"])
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    refused = client.post(
        f"/t/{cup['id']}/matches/{match_id}/events",
        data={**csrf(), "new_kind": "ban", "new_side": "1", "new_map": str(cup["maps"]["NM2"])},
    )
    page = client.get(review_url(cup, match_id)).text

    assert refused.status_code == 409
    assert "Correct events" not in page and "This tournament is archived" in page


def test_a_match_of_another_tournament_is_not_found(client, login, db, cup, match_id):
    foreign = other_tournament(db)
    login(CAPTAIN)

    assert client.get(f"/t/{foreign}/matches/{match_id}/review").status_code == 404
    assert client.get(review_url(cup, 99999)).status_code == 404


# --- reading the lobby from the page ----------------------------------------------------------


@pytest.fixture
def linked(db, osu, match_id):
    db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (LOBBY, match_id))
    osu.add_match(LOBBY)
    osu.add_game(LOBBY, NM1, [(CAPTAIN, 900), (RIVAL_A, 800)])
    return match_id


def test_a_captain_reads_the_lobby_and_the_picks_appear(client, login, csrf, db, cup, linked):
    login(CAPTAIN)

    response = client.post(review_url(cup, linked, "/refresh"), data=csrf(), follow_redirects=True)

    assert "Lobby read: 1 played maps found." in response.text
    stored = events_repo.list_for_match(db, linked)
    assert [(e.seq, e.pool_map_id, e.winner_side, e.source) for e in stored] == [
        (100, cup["maps"]["NM1"], 1, "mp")
    ]


def test_a_lobby_osu_does_not_have_is_reported_and_changes_nothing(
    client, login, csrf, db, cup, linked
):
    db.execute("UPDATE matches SET mp_match_id = 4040 WHERE id = ?", (linked,))
    login(CAPTAIN)

    response = client.post(review_url(cup, linked, "/refresh"), data=csrf(), follow_redirects=True)

    assert "osu! has no lobby with this id" in response.text
    assert events_repo.list_for_match(db, linked) == []


def test_osu_being_down_is_reported_and_changes_nothing(client, login, csrf, db, cup, linked, osu):
    osu.unavailable_paths.add(f"/api/v2/matches/{LOBBY}")
    login(CAPTAIN)

    response = client.post(review_url(cup, linked, "/refresh"), data=csrf(), follow_redirects=True)

    assert "osu! did not respond" in response.text
    assert events_repo.list_for_match(db, linked) == []


def test_a_match_without_a_link_cannot_be_read(client, login, csrf, cup, match_id):
    login(CAPTAIN)

    response = client.post(review_url(cup, match_id, "/refresh"), data=csrf())

    assert response.status_code == 409
    assert "no multiplayer link" in response.text


def test_an_archived_tournament_does_not_read_lobbies(client, login, csrf, db, cup, linked, osu):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    response = client.post(review_url(cup, linked, "/refresh"), data=csrf())

    assert response.status_code == 409
    assert osu.match_requests(LOBBY) == []


@pytest.fixture
def others_match(db, cup):
    """A finished match between two teams of the cup that is not ours, with a lobby."""
    third = add_team(db, cup["id"], "Third Wheel")
    match_id = add_match(
        db, cup["id"], cup["rivals"], third, "2026-11-13T18:00:00Z", stage_id=cup["stage"]
    )
    db.execute(
        "UPDATE matches SET status = 'done', mp_match_id = ? WHERE id = ?", (LOBBY, match_id)
    )
    return match_id


def test_a_captain_may_not_correct_or_read_a_match_our_team_does_not_play(
    client, login, csrf, db, cup, others_match, osu
):
    osu.add_match(LOBBY)
    osu.add_game(LOBBY, NM1, [(CAPTAIN, 900), (RIVAL_A, 800)])
    login(CAPTAIN)
    base = f"/t/{cup['id']}/matches/{others_match}"

    corrected = client.post(
        f"{base}/events",
        data={**csrf(), "new_kind": "ban", "new_side": "1", "new_map": str(cup["maps"]["NM1"])},
    )
    refreshed = client.post(f"{base}/review/refresh", data=csrf())
    page = client.get(f"{base}/review").text

    assert (corrected.status_code, refreshed.status_code) == (409, 409)
    assert events_repo.list_for_match(db, others_match) == []
    assert osu.match_requests(LOBBY) == []
    assert "Correct events" not in page and "Read lobby again" not in page


def test_an_admin_may_correct_and_read_any_match(client, login, csrf, db, cup, others_match, osu):
    add_user(db, ADMIN_ID, "AdminAmy")
    osu.add_match(LOBBY)
    osu.add_game(LOBBY, NM1, [(CAPTAIN, 900), (RIVAL_A, 800)])
    login(ADMIN_ID)
    base = f"/t/{cup['id']}/matches/{others_match}"

    page = client.get(f"{base}/review").text
    corrected = client.post(
        f"{base}/events",
        data={**csrf(), "new_kind": "ban", "new_side": "1", "new_map": str(cup["maps"]["NM1"])},
        follow_redirects=False,
    )
    refreshed = client.post(f"{base}/review/refresh", data=csrf(), follow_redirects=False)

    assert "Correct events" in page and "Read lobby again" in page
    assert (corrected.status_code, refreshed.status_code) == (303, 303)
    assert {e.source for e in events_repo.list_for_match(db, others_match)} == {"manual", "mp"}


def test_the_page_reads_a_lobby_at_interactive_priority(
    client, login, csrf, cup, linked, monkeypatch
):
    seen = []
    original = OsuClient.fetch_match

    async def fetch_match(self, match_id, *args, **kwargs):
        seen.append(kwargs.get("priority"))
        return await original(self, match_id, *args, **kwargs)

    monkeypatch.setattr(OsuClient, "fetch_match", fetch_match)
    login(CAPTAIN)

    client.post(review_url(cup, linked, "/refresh"), data=csrf())

    assert seen == [Priority.INTERACTIVE]


# --- the match page and the opponent page -----------------------------------------------------


def test_the_match_page_links_to_the_review_and_the_opponent(client, login, cup, match_id):
    login(MATE)

    text = client.get(f"/t/{cup['id']}/matches/{match_id}").text

    assert f"/t/{cup['id']}/matches/{match_id}/review" in text
    assert f"/t/{cup['id']}/opponents/{cup['rivals']}" in text


def test_the_opponent_page_shows_habits_rounds_and_maps_never_picked(
    client, login, db, cup, match_id
):
    maps = cup["maps"]
    played = add_game(db, cup, when="2026-11-01T18:00:00Z", status="done")
    add_event(db, played, 100, "pick", 2, maps["HD1"])
    add_event(db, played, 101, "pick", 1, maps["NM1"])
    login(MATE)

    page = client.get(f"/t/{cup['id']}/opponents/{cup['rivals']}")

    assert page.status_code == 200
    text = page.text
    assert "By mod bracket" in text and "By round" in text and "Round of 16" in text
    assert "Picked first" in text and "100%" in text
    assert "Never picked" in text


def test_the_opponent_page_without_history_says_what_to_do(client, login, cup):
    login(MATE)

    text = client.get(f"/t/{cup['id']}/opponents/{cup['rivals']}").text

    assert "No picks or bans are recorded for Rival Crew yet." in text
    assert "By mod bracket" not in text


def test_a_team_of_another_tournament_is_not_found(client, login, db, cup):
    stranger = add_team(db, other_tournament(db), "Elsewhere")
    login(MATE)

    assert client.get(f"/t/{cup['id']}/opponents/{stranger}").status_code == 404
    assert client.get(f"/t/{cup['id']}/opponents/99999").status_code == 404
