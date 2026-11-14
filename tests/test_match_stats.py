"""Match stats and the Results page: the per-player scores a lobby read leaves behind, the stats
built from them, and the pages that show them."""

import asyncio
from datetime import UTC, datetime

import pytest

from app.repos import matches as matches_repo
from app.repos import scores as scores_repo
from app.services import history, match_stats
from tests.matchup_support import (
    CAPTAIN,
    HD1,
    MATE,
    NM1,
    NM2,
    RIVAL_A,
    RIVAL_B,
    add_game,
    build_cup,
)
from tests.support import add_member, add_stage, add_team, add_tournament

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
STAND_IN = 9999
WARM_UP = 777777


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def cup(db):
    return build_cup(db)


@pytest.fixture
def match(db, cup):
    match_id = add_game(db, cup, status="done", score1=2, score2=1)
    db.execute("UPDATE matches SET mp_match_id = 777 WHERE id = ?", (match_id,))
    return matches_repo.get(db, match_id)


def tournament_of(db, cup):
    from app.repos import tournaments as tournaments_repo

    return tournaments_repo.get(db, cup["id"])


def play(osu, beatmap, *scores, lobby_id=777):
    osu.add_game(lobby_id, beatmap, list(scores))


def read_lobby(db, scan_client, match):
    return run(history.refresh_from_lobby(db, scan_client, match, NOW))


@pytest.fixture
def played(db, cup, match, osu, scan_client):
    """A best-of-3 lobby: team 1 (ours) wins NM1 and HD1, loses NM2. A stand-in plays for ours on
    NM2 and a warm-up map is played first."""
    osu.add_match(777)
    play(osu, WARM_UP, (CAPTAIN, 1), (RIVAL_A, 2))
    play(osu, NM1, (CAPTAIN, 900_000), (MATE, 800_000), (RIVAL_A, 700_000), (RIVAL_B, 600_000))
    play(osu, NM2, (CAPTAIN, 500_000), (STAND_IN, 400_000), (RIVAL_A, 800_000), (RIVAL_B, 700_000))
    play(osu, HD1, (CAPTAIN, 950_000), (MATE, 850_000), (RIVAL_A, 700_000), (RIVAL_B, 650_000))
    read_lobby(db, scan_client, match)
    return match


def stored_scores(db):
    rows = db.execute(
        "SELECT beatmap_id, osu_id, score, source_ref FROM scores WHERE source = 'mp' "
        "ORDER BY beatmap_id, osu_id"
    ).fetchall()
    return [tuple(row) for row in rows]


# --- storing the players' scores ---------------------------------------------------------------


def test_reading_a_lobby_stores_the_scores_of_both_teams_rostered_players(db, played):
    found = stored_scores(db)

    assert {(beatmap, osu_id) for beatmap, osu_id, *_ in found} == {
        (NM1, CAPTAIN),
        (NM1, MATE),
        (NM1, RIVAL_A),
        (NM1, RIVAL_B),
        (NM2, CAPTAIN),
        (NM2, RIVAL_A),
        (NM2, RIVAL_B),
        (HD1, CAPTAIN),
        (HD1, MATE),
        (HD1, RIVAL_A),
        (HD1, RIVAL_B),
    }


def test_stand_ins_and_warm_up_maps_are_not_stored(db, played):
    found = stored_scores(db)

    assert STAND_IN not in {osu_id for _, osu_id, *_ in found}
    assert WARM_UP not in {beatmap for beatmap, *_ in found}


def test_the_stored_scores_belong_to_the_lobby_and_game(db, played):
    refs = {ref for *_, ref in stored_scores(db)}

    assert refs and all(ref.startswith("777:") for ref in refs)
    assert len(scores_repo.list_mp_for_lobby(db, 777)) == len(stored_scores(db))
    assert scores_repo.list_mp_for_lobby(db, 77) == []  # another lobby whose id starts alike


def test_reading_the_lobby_twice_adds_no_duplicate_rows(db, scan_client, played):
    before = stored_scores(db)

    read_lobby(db, scan_client, played)

    assert stored_scores(db) == before


# --- build_match_stats -------------------------------------------------------------------------


def test_the_maps_come_in_play_order_with_winner_and_totals(db, cup, played):
    stats = match_stats.build_match_stats(db, played)

    assert [m.pool_map.slot for m in stats.maps] == ["NM1", "NM2", "HD1"]
    assert [m.winner_name for m in stats.maps] == ["Alpha Squad", "Rival Crew", "Alpha Squad"]
    assert [(m.team1_total, m.team2_total) for m in stats.maps] == [
        (1_700_000, 1_300_000),
        (500_000, 1_500_000),  # a head-to-head lobby gives a stand-in no team to count for
        (1_800_000, 1_350_000),
    ]
    assert (stats.has_lobby, stats.lobby_read) == (True, True)
    assert stats.stage.name == "Round of 16"


def test_each_map_lists_the_scores_of_the_rostered_players(db, played):
    nm2 = match_stats.build_match_stats(db, played).maps[1]

    assert {(s.name, s.side, s.score) for s in nm2.scores} == {
        ("Cap", 1, 500_000),
        ("RivalA", 2, 800_000),
        ("RivalB", 2, 700_000),
    }
    assert all(s.accuracy == 98.0 and s.passed for s in nm2.scores)


def test_player_lines_have_the_means_and_the_maps_won(db, played):
    lines = {line.name: line for line in match_stats.build_match_stats(db, played).players}

    cap = lines["Cap"]
    assert (cap.side, cap.maps_played, cap.maps_won) == (1, 3, 2)
    assert cap.mean_score == pytest.approx((900_000 + 500_000 + 950_000) / 3)
    assert cap.mean_accuracy == 98.0
    assert (lines["Mate"].maps_played, lines["Mate"].maps_won) == (2, 2)
    assert (lines["RivalA"].side, lines["RivalA"].maps_won) == (2, 1)
    assert [line.side for line in match_stats.build_match_stats(db, played).players] == [1, 1, 2, 2]


def test_a_map_played_again_uses_the_last_game(db, cup, match, osu, scan_client):
    osu.add_match(777)
    play(osu, NM1, (CAPTAIN, 100_000), (RIVAL_A, 900_000))  # lost, then restarted
    play(osu, NM1, (CAPTAIN, 900_000), (RIVAL_A, 100_000))
    read_lobby(db, scan_client, match)

    stats = match_stats.build_match_stats(db, match)

    assert len(stats.maps) == 1
    assert [(s.name, s.score) for s in stats.maps[0].scores] == [
        ("Cap", 900_000),
        ("RivalA", 100_000),
    ]
    assert stats.maps[0].winner_name == "Alpha Squad"


def test_a_match_without_a_lobby_has_no_stats(db, cup):
    bare = matches_repo.get(db, add_game(db, cup))

    stats = match_stats.build_match_stats(db, bare)

    assert (stats.has_lobby, stats.lobby_read, stats.maps, stats.players) == (False, False, [], [])


def test_a_lobby_that_was_not_read_yet_says_so(db, match):
    stats = match_stats.build_match_stats(db, match)

    assert (stats.has_lobby, stats.lobby_read, stats.maps) == (True, False, [])


def test_a_map_without_stored_scores_has_an_empty_list(db, played):
    db.execute("DELETE FROM scores WHERE source = 'mp' AND beatmap_id = ?", (NM1,))

    first = match_stats.build_match_stats(db, played).maps[0]

    assert first.scores == [] and first.winner_name == "Alpha Squad"


# --- tournament_results ------------------------------------------------------------------------


def test_the_results_aggregate_only_our_players_over_played_matches_with_a_lobby(db, cup, played):
    results = match_stats.tournament_results(db, tournament_of(db, cup), NOW)

    assert [view.match.id for view in results.matches] == [played.id]
    assert {line.name for line in results.players} == {"Cap", "Mate"}
    cap = next(line for line in results.players if line.name == "Cap")
    assert (cap.side, cap.maps_played, cap.maps_won) == (None, 3, 2)


def test_our_side_may_be_team_two(db, cup, osu, scan_client):
    match_id = add_game(db, cup, ours_first=False, status="done", score1=1, score2=2)
    db.execute("UPDATE matches SET mp_match_id = 778 WHERE id = ?", (match_id,))
    osu.add_match(778)
    play(osu, NM1, (CAPTAIN, 900_000), (RIVAL_A, 700_000), lobby_id=778)
    read_lobby(db, scan_client, matches_repo.get(db, match_id))

    results = match_stats.tournament_results(db, tournament_of(db, cup), NOW)

    assert [(line.name, line.maps_won) for line in results.players] == [("Cap", 1)]


def test_without_our_team_there_are_no_player_totals(db, cup, played):
    db.execute("UPDATE tournaments SET our_team_id = NULL WHERE id = ?", (cup["id"],))

    results = match_stats.tournament_results(db, tournament_of(db, cup), NOW)

    assert len(results.matches) == 1 and results.players == []


def test_a_match_that_has_not_been_played_is_not_in_the_results(db, cup, played):
    add_game(db, cup, when="2026-12-24T18:00:00Z")

    results = match_stats.tournament_results(db, tournament_of(db, cup), NOW)

    assert [view.match.id for view in results.matches] == [played.id]


# --- the pages ---------------------------------------------------------------------------------


def stats_url(cup, match):
    return f"/t/{cup['id']}/matches/{match.id}/stats"


def test_members_see_the_results_and_the_match_stats(client, login, cup, played):
    login(MATE)

    results = client.get(f"/t/{cup['id']}/results")
    stats = client.get(stats_url(cup, played))

    assert results.status_code == stats.status_code == 200
    assert "Played matches" in results.text and "Our players" in results.text
    assert f'href="{stats_url(cup, played)}"' in results.text
    for text in ("Alpha Squad vs Rival Crew", "NM1", "Players", "stand-ins still count"):
        assert text in stats.text
    assert "1,700,000" in stats.text and "No player scores for this map" not in stats.text


def test_the_tournament_menu_has_results_between_schedule_and_pickems(client, login, cup):
    login(MATE)

    html = client.get(f"/t/{cup['id']}/results").text

    nav = html.split('class="side-nav__list"')[1]
    assert nav.index(">Schedule<") < nav.index(">Results<") < nav.index(">Pickems<")
    assert 'aria-current="page">Results' in nav


def test_the_schedule_links_each_played_match_to_its_stats(client, login, cup, played):
    login(MATE)

    html = client.get(f"/t/{cup['id']}/schedule").text

    assert f'href="{stats_url(cup, played)}"' in html and ">Compare<" in html


def test_the_review_page_links_to_the_stats(client, login, cup, played):
    login(CAPTAIN)

    assert (
        f'href="{stats_url(cup, played)}"'
        in client.get(f"/t/{cup['id']}/matches/{played.id}/review").text
    )


def test_a_match_without_a_lobby_explains_why_there_are_no_stats(client, login, db, cup):
    bare = matches_repo.get(db, add_game(db, cup))
    login(MATE)

    assert "has no multiplayer link, so there are no stats" in client.get(stats_url(cup, bare)).text


def test_a_lobby_not_read_yet_explains_and_a_captain_can_read_it(client, login, cup, match):
    login(CAPTAIN)

    html = client.get(stats_url(cup, match)).text

    assert "The lobby has not been read yet" in html
    assert 'name="return_to" value="stats"' in html and "Read lobby again" in html


def test_a_player_has_no_read_lobby_button(client, login, cup, match):
    login(MATE)

    assert "Read lobby again" not in client.get(stats_url(cup, match)).text


def test_a_match_of_another_tournament_is_not_found(client, login, db, cup, match):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, MATE, "player")
    login(MATE)

    assert client.get(f"/t/{other}/matches/{match.id}/stats").status_code == 404


def test_someone_outside_the_tournament_gets_the_usual_refusal(client, login, cup, played):
    login(4242)

    assert client.get(f"/t/{cup['id']}/results").status_code == 404
    assert client.get(stats_url(cup, played)).status_code == 404


def test_reading_the_lobby_from_the_stats_page_returns_there(client, login, csrf, cup, match, osu):
    osu.add_match(777)
    play(osu, NM1, (CAPTAIN, 900_000), (RIVAL_A, 700_000))
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/matches/{match.id}/review/refresh",
        data={**csrf(), "return_to": "stats"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == stats_url(cup, match)
    assert "NM1" in client.get(stats_url(cup, match)).text


def test_reading_the_lobby_from_the_review_page_still_returns_to_review(
    client, login, csrf, cup, match, osu
):
    osu.add_match(777)
    play(osu, NM1, (CAPTAIN, 900_000), (RIVAL_A, 700_000))
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/matches/{match.id}/review/refresh",
        data=csrf(),
        follow_redirects=False,
    )

    assert response.headers["location"] == f"/t/{cup['id']}/matches/{match.id}/review"


def test_the_empty_results_page_says_nothing_was_played(client, login, db):
    tid = add_tournament(db, "Quiet Cup")
    add_member(db, tid, MATE, "player")
    add_stage(db, tid)
    add_team(db, tid, "Us", [(MATE, "Mate")], ours=True)
    login(MATE)

    html = client.get(f"/t/{tid}/results").text

    assert "No match has been played yet." in html and "Our players" not in html


def test_scores_of_players_on_neither_roster_get_no_player_row_but_the_totals_stay(db, cup, played):
    db.execute(
        "INSERT INTO scores (beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, passed, "
        "source, source_ref, created_at) VALUES (?, 5555, 0, '', 999999, 'v2', 90.0, 1, 'mp', "
        "'777:601', '2026-11-14T12:00:00Z')",
        (NM1,),
    )

    stats = match_stats.build_match_stats(db, played)

    assert 5555 not in {s.osu_id for m in stats.maps for s in m.scores}
    assert 5555 not in {line.osu_id for line in stats.players}
    assert all(s.side in (1, 2) for m in stats.maps for s in m.scores)
    assert (stats.maps[0].team1_total, stats.maps[0].team2_total) == (1_700_000, 1_300_000)
    results = match_stats.tournament_results(db, tournament_of(db, cup), NOW)
    assert 5555 not in {line.osu_id for line in results.players}
