"""The Lobbies page: who sees what, the filters, and adding a lobby by link."""

import pytest

from app.repos import lobbies as lobbies_repo
from tests.support import (
    add_member,
    add_pool_map,
    add_stage,
    add_team,
    add_tournament,
)

CAPTAIN, PLAYER, GUEST, STRANGER, RIVAL, OTHER_RIVAL = 2001, 2002, 2003, 2009, 3001, 3002
NOW = "2026-11-12T12:30:15Z"


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, GUEST, "guest")
    ours = add_team(db, tid, "Ours", [(CAPTAIN, "CaptainCarl"), (PLAYER, "PlayerPia")], ours=True)
    theirs = add_team(db, tid, "Theirs", [(RIVAL, "RivalRon"), (OTHER_RIVAL, "RivalRae")])
    quals = add_stage(db, tid, "Qualifiers", 1)
    finals = add_stage(db, tid, "Finals", 2)
    maps = {
        "NM1": add_pool_map(db, quals, "NM1", beatmap_id=1001),
        "HD1": add_pool_map(db, quals, "HD1", beatmap_id=1002, required_mods="HD"),
        "FM1": add_pool_map(db, finals, "FM1", beatmap_id=1003, freemod=True),
    }
    return {"id": tid, "ours": ours, "theirs": theirs, "quals": quals, "finals": finals, **maps}


def store(db, cup, mp_id, name, slots, players, started="2026-11-10T18:00:00Z", complete=True):
    """A stored lobby with hits on the given slots and the given registered players."""
    lobbies_repo.upsert(db, mp_id, name, started, None, NOW, complete, "scan")
    for game, slot in enumerate(slots):
        lobbies_repo.insert_hit(db, mp_id, mp_id * 10 + game, cup[slot], started, "")
    for osu_id, team in players:
        lobbies_repo.insert_player(db, mp_id, cup["id"], osu_id, cup[team])


@pytest.fixture
def stored(db, cup):
    store(db, cup, 5001, "just practice", ["NM1", "HD1"], [(CAPTAIN, "ours"), (RIVAL, "theirs")])
    store(
        db,
        cup,
        5002,
        "finals warmup",
        ["FM1"],
        [(PLAYER, "ours"), (OTHER_RIVAL, "theirs")],
        started="2026-11-11T18:00:00Z",
        complete=False,
    )


def page(client, cup, query=""):
    return client.get(f"/t/{cup['id']}/lobbies{query}")


# --- who may see it ---


def test_a_stranger_sees_nothing_and_an_anonymous_visitor_is_sent_to_login(
    client, login, cup, stored
):
    anonymous = client.get(f"/t/{cup['id']}/lobbies", follow_redirects=False)
    assert anonymous.status_code == 303

    login(STRANGER)
    assert page(client, cup).status_code == 404


@pytest.mark.parametrize("who", [GUEST, PLAYER, CAPTAIN])
def test_every_member_reads_the_lobbies(client, login, cup, stored, who):
    login(who)

    response = page(client, cup)

    assert response.status_code == 200
    assert "just practice" in response.text


def test_the_navigation_has_lobbies_and_coverage(client, login, cup):
    login(PLAYER)

    text = page(client, cup).text

    assert f'href="/t/{cup["id"]}/lobbies"' in text
    assert f'href="/t/{cup["id"]}/coverage"' in text


# --- the table ---


def test_each_lobby_shows_link_name_date_maps_and_players_by_team(client, login, cup, stored):
    login(PLAYER)

    text = page(client, cup).text

    assert 'href="https://osu.ppy.sh/community/matches/5001"' in text
    assert "just practice" in text and "finals warmup" in text
    assert text.index("5002") < text.index("5001")  # newest first
    first = text.split("5001</a>")[1].split("</tr>")[0]
    assert ">NM1<" in first and ">HD1<" in first and ">FM1<" not in first
    assert "Ours:" in first and "Theirs:" in first
    assert first.index("Ours:") < first.index("Theirs:")  # our team first


def test_our_players_are_marked_in_words_and_the_others_are_not(client, login, cup, stored):
    login(PLAYER)

    first = page(client, cup).text.split("5001</a>")[1].split("</tr>")[0]

    assert '<strong>CaptainCarl</strong> <span class="muted">(our team)</span>' in first
    assert "RivalRon" in first and "<strong>RivalRon" not in first


def test_a_lobby_still_being_read_is_said_to_be_open_and_the_source_is_named(
    client, login, cup, stored
):
    login(PLAYER)

    text = page(client, cup).text

    assert "Still open or being read" in text and "Finished" in text
    assert "Found by the scanner" in text


def test_a_lobby_of_another_tournament_is_not_shown(client, login, db, cup, stored):
    other = add_tournament(db, "Other Cup", acronym="OC")
    other_stage = add_stage(db, other)
    other_map = add_pool_map(db, other_stage, "NM1", beatmap_id=7777)
    lobbies_repo.upsert(db, 6001, "elsewhere", NOW, None, NOW, True, "scan")
    lobbies_repo.insert_hit(db, 6001, 1, other_map, NOW, "")
    login(PLAYER)

    assert "elsewhere" not in page(client, cup).text


def test_a_lobby_without_a_hit_that_was_added_for_this_tournament_is_listed(client, login, db, cup):
    lobbies_repo.upsert(db, 6002, "added by hand", NOW, None, NOW, True, "manual")
    lobbies_repo.insert_player(db, 6002, cup["id"], PLAYER, cup["ours"])
    login(PLAYER)

    text = page(client, cup).text

    assert "added by hand" in text and "None" in text and "Added by hand" in text


def test_an_empty_page_says_what_to_do(client, login, cup):
    login(PLAYER)

    assert "No lobby with a pool map has been found yet" in page(client, cup).text


def test_a_name_with_markup_is_escaped(client, login, db, cup):
    store(db, cup, 5003, "<script>alert(1)</script>", ["NM1"], [])
    login(PLAYER)

    text = page(client, cup).text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


# --- the filters ---


def test_the_stage_filter_keeps_lobbies_with_a_hit_in_that_stage(client, login, cup, stored):
    login(PLAYER)

    text = page(client, cup, f"?stage={cup['finals']}").text

    assert "finals warmup" in text and "just practice" not in text


def test_the_player_filter_keeps_lobbies_that_player_was_found_in(client, login, cup, stored):
    login(PLAYER)

    text = page(client, cup, f"?player={RIVAL}").text

    assert "just practice" in text and "finals warmup" not in text


def test_the_filters_combine_and_an_empty_result_says_so(client, login, cup, stored):
    login(PLAYER)

    text = page(client, cup, f"?stage={cup['finals']}&player={CAPTAIN}").text

    assert "No lobby matches these filters." in text


def test_the_player_filter_offers_everyone_found_with_their_names(client, login, cup, stored):
    login(PLAYER)

    text = page(client, cup, f"?player={RIVAL}").text

    assert f'<option value="{RIVAL}" selected>RivalRon</option>' in text
    assert f'<option value="{CAPTAIN}">CaptainCarl</option>' in text


def test_a_filter_naming_a_stage_of_another_tournament_is_ignored(client, login, cup, stored):
    login(PLAYER)

    text = page(client, cup, "?stage=99999&player=abc").text

    assert "just practice" in text and "finals warmup" in text


# --- adding a link ---


def link(client, cup, csrf, value):
    return client.post(
        f"/t/{cup['id']}/lobbies", data={**csrf(), "link": value}, follow_redirects=False
    )


def fake_lobby(osu, mp_id=7100, beatmap_id=1001, user=PLAYER):
    osu.add_match(mp_id, "found by link")
    osu.add_game(mp_id, beatmap_id, [(user, 880_000)])


def test_a_player_adds_a_lobby_by_link_and_it_shows_up(client, login, csrf, osu, db, cup):
    fake_lobby(osu)
    login(PLAYER)

    response = link(client, cup, csrf, "https://osu.ppy.sh/community/matches/7100")

    assert response.status_code == 303
    text = page(client, cup).text
    assert "Lobby 7100 saved: 1 games on pool maps found." in text
    assert "found by link" in text and "Added by hand" in text
    assert (
        db.execute("SELECT discovered_by FROM lobbies WHERE mp_match_id = 7100").fetchone()[0]
        == "manual"
    )
    assert db.execute("SELECT COUNT(*) FROM scores WHERE source = 'mp'").fetchone()[0] == 1


def test_a_bare_match_id_is_accepted_too(client, login, csrf, osu, db, cup):
    fake_lobby(osu)
    login(PLAYER)

    link(client, cup, csrf, "7100")

    assert lobbies_repo.get(db, 7100) is not None


def test_a_finished_lobby_is_not_read_again(client, login, csrf, osu, db, cup):
    fake_lobby(osu)
    login(PLAYER)
    link(client, cup, csrf, "7100")
    requests_before = len(osu.match_requests(7100))

    again = link(client, cup, csrf, "7100")

    assert again.status_code == 303
    assert len(osu.match_requests(7100)) == requests_before  # a finished lobby is not read again
    assert "Lobby 7100 is already saved." in page(client, cup).text
    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 1


def test_a_lobby_without_pool_maps_is_still_saved_and_says_so(client, login, csrf, osu, cup):
    fake_lobby(osu, beatmap_id=424242)
    login(PLAYER)

    link(client, cup, csrf, "7100")

    assert "Lobby 7100 saved: no pool map found." in page(client, cup).text


@pytest.mark.parametrize("value", ["", "hello", "https://osu.ppy.sh/beatmaps/12", "12345678901234"])
def test_a_text_that_is_no_lobby_link_is_refused_and_kept_in_the_field(
    client, login, csrf, osu, cup, value
):
    login(PLAYER)

    response = link(client, cup, csrf, value)

    assert response.status_code == 422
    assert "Paste an osu! multiplayer link" in response.text
    assert osu.requests == []


def test_the_refused_text_stays_in_the_field(client, login, csrf, cup):
    login(PLAYER)

    response = link(client, cup, csrf, "not a link")

    assert 'value="not a link"' in response.text


def test_a_lobby_osu_does_not_have_is_refused_with_a_reason(client, login, csrf, osu, db, cup):
    login(PLAYER)

    response = link(client, cup, csrf, "999999")

    assert response.status_code == 422
    assert "osu! has no lobby with this id, or it is private." in response.text
    assert 'value="999999"' in response.text
    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 0


def test_when_osu_is_down_nothing_is_saved_and_the_page_says_to_retry(
    client, login, csrf, osu, db, cup
):
    osu.match_statuses[7100] = 500
    login(PLAYER)

    response = link(client, cup, csrf, "7100")

    assert response.status_code == 502
    assert "osu! did not respond" in response.text
    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 0


def test_a_guest_cannot_add_a_lobby_and_does_not_see_the_form(client, login, csrf, osu, cup):
    fake_lobby(osu)
    login(GUEST)
    assert "Add a lobby link" not in page(client, cup).text

    response = link(client, cup, csrf, "7100")

    assert response.status_code == 403
    assert osu.requests == []


def test_an_archived_tournament_takes_no_new_lobbies(client, login, csrf, osu, db, cup):
    fake_lobby(osu)
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    response = link(client, cup, csrf, "7100")

    assert response.status_code == 409
    assert "archived" in page(client, cup).text


def test_adding_needs_the_csrf_token(client, login, osu, cup):
    fake_lobby(osu)
    login(PLAYER)

    response = client.post(f"/t/{cup['id']}/lobbies", data={"link": "7100"})

    assert response.status_code == 403
    assert osu.requests == []


def test_a_person_may_add_twenty_lobbies_an_hour(client, login, csrf, osu, cup):
    for number in range(21):
        osu.add_match(7200 + number, end_time=None)
    login(PLAYER)

    statuses = [link(client, cup, csrf, str(7200 + n)).status_code for n in range(21)]

    assert statuses == [303] * 20 + [429]
