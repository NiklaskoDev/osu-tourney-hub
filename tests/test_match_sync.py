"""Matches from wybin: parsing the endpoint and storing matches with the right teams and stage.

Match fields are confirmed for 1v1 tournaments only. The team fixture (`matches_team.json`) is
synthetic: it assumes `opponentOne` and `opponentTwo` are team objects.
"""

import asyncio
import copy

import pytest

from app.clients.http import make_http_client
from app.clients.wybin import WybinClient
from app.clients.wybin_models import (
    WybinFormatError,
    WybinNotFound,
    WybinSide,
    WybinUnavailable,
    parse_matches,
)
from app.services.wybin_sync import (
    TournamentArchivedError,
    WybinSyncError,
    import_tournament,
    sync_active,
    sync_tournament,
)
from tests.support import ADMIN_ID, add_user, load_fixture


@pytest.fixture
def fake(wybin):
    wybin.load_defaults()
    return wybin


@pytest.fixture
def client_for(fake):
    async def no_sleep(seconds: float) -> None:
        return None

    return WybinClient(make_http_client(fake.transport), 0, sleep=no_sleep)


@pytest.fixture
def conn(db):
    add_user(db, ADMIN_ID, "AdminAmy")
    return db


def run(coroutine):
    return asyncio.run(coroutine)


def do_import(conn, client, clock, text="synthetic-cup"):
    return run(import_tournament(conn, client, text, ADMIN_ID, clock))


def matches(conn):
    rows = conn.execute(
        "SELECT m.*, s.name AS stage_name, t1.name AS team1, t2.name AS team2 FROM matches m "
        "LEFT JOIN stages s ON s.id = m.stage_id "
        "LEFT JOIN teams t1 ON t1.id = m.team1_id LEFT JOIN teams t2 ON t2.id = m.team2_id "
        "ORDER BY m.wybin_match_id"
    ).fetchall()
    return {row["wybin_match_id"]: row for row in rows}


# --- parsing -----------------------------------------------------------------------------------


def test_matches_are_read_from_every_stage():
    found = parse_matches(load_fixture("matches_team"))

    assert [m.wybin_match_id for m in found] == [5004, 5001, 5002, 5003]
    assert [m.wybin_stage_id for m in found] == [701, 702, 702, 702]


def test_a_match_keeps_its_label_time_score_and_lobby_link():
    played = parse_matches(load_fixture("matches_team"))[0]

    assert played.label == "Qualifier decider"
    assert played.scheduled_at == "2026-11-10T18:00:00Z"  # epoch milliseconds, read as UTC
    assert (played.score1, played.score2) == (5, 3)
    assert played.multiplayer_link == "https://osu.ppy.sh/community/matches/100200300"


def test_an_unscheduled_match_has_no_time_and_no_scores():
    unscheduled = parse_matches(load_fixture("matches_team"))[3]

    assert unscheduled.scheduled_at is None
    assert (unscheduled.score1, unscheduled.score2) == (None, None)
    assert unscheduled.multiplayer_link is None


def test_a_team_side_is_named_by_the_opponent_object():
    found = parse_matches(load_fixture("matches_team"))[0]

    assert found.side1 == WybinSide(opponent_ids=(801,))
    assert found.side2 == WybinSide(opponent_ids=(802,))


def test_a_solo_side_carries_wybin_ids_and_osu_ids():
    first = parse_matches(load_fixture("matches_solo"))[0]

    assert first.side1 == WybinSide(opponent_ids=(11,), player_ids=(11,), osu_ids=(1001,))
    assert first.side2.osu_ids == (4001,)


def test_a_tournament_without_matches_gives_an_empty_list():
    assert parse_matches({"id": 1, "stages": []}) == []
    assert parse_matches({"id": 1}) == []
    assert parse_matches({"id": 1, "stages": [{"id": 5, "matches": None}]}) == []


def test_a_match_without_an_id_is_skipped_but_the_rest_are_kept():
    data = load_fixture("matches_team")
    del data["stages"][1]["matches"][0]["id"]

    assert [m.wybin_match_id for m in parse_matches(data)] == [5004, 5002, 5003]


def test_a_list_where_no_match_can_be_read_is_a_format_error():
    data = {"stages": [{"id": 1, "matches": [{"label": "no id"}, "nonsense"]}]}

    with pytest.raises(WybinFormatError):
        parse_matches(data)


@pytest.mark.parametrize(
    "data", [[], "text", None, {"stages": "x"}, {"stages": [{"matches": "x"}]}]
)
def test_an_unexpected_shape_is_a_format_error(data):
    with pytest.raises(WybinFormatError):
        parse_matches(data)


def test_the_bans_of_each_side_are_read_as_beatmap_ids():
    data = load_fixture("matches_team")
    played = data["stages"][0]["matches"][0]
    played["opponentOneBans"] = [910001, {"beatmapId": 910002}, {"beatmap": {"beatmapId": 910003}}]
    played["opponentTwoBans"] = [910004, 910004, "junk", None, True, {"x": 1}]

    found = parse_matches(data)[0]

    assert found.bans1 == (910001, 910002, 910003)
    assert found.bans2 == (910004,)  # a repeated or unreadable entry is skipped


@pytest.mark.parametrize("value", [None, "910001", 5, {"a": 1}])
def test_ban_lists_that_are_not_lists_are_empty(value):
    data = load_fixture("matches_team")
    data["stages"][0]["matches"][0]["opponentOneBans"] = value

    assert parse_matches(data)[0].bans1 == ()


@pytest.mark.parametrize("value", [None, "12", -1, True, 1.5])
def test_a_score_must_be_a_whole_number_not_below_zero(value):
    data = load_fixture("matches_team")
    data["stages"][0]["matches"][0]["opponentOneScore"] = value

    assert parse_matches(data)[0].score1 is None


def test_odd_dates_become_no_time():
    data = load_fixture("matches_team")
    data["stages"][0]["matches"][0]["date"] = "tomorrow"

    assert parse_matches(data)[0].scheduled_at is None


def test_a_missing_side_is_empty():
    data = load_fixture("matches_team")
    data["stages"][0]["matches"][0]["opponentTwo"] = None

    assert parse_matches(data)[0].side2 == WybinSide()


# --- the client --------------------------------------------------------------------------------


def test_the_client_asks_the_matches_endpoint_for_the_tournament(fake, client_for):
    found = run(client_for.get_matches(9001))

    assert len(found) == 4
    assert fake.paths() == ["/api/v1/tournament-matches/9001"]


def test_a_missing_matches_endpoint_is_not_found(fake, client_for):
    fake.missing_kinds.add("tournament-matches")

    with pytest.raises(WybinNotFound):
        run(client_for.get_matches(9001))


# --- storing -----------------------------------------------------------------------------------


def test_an_import_stores_matches_with_teams_stage_and_result(conn, client_for, clock):
    do_import(conn, client_for, clock)

    stored = matches(conn)

    assert set(stored) == {5001, 5002, 5003, 5004}
    final = stored[5004]
    assert (final["team1"], final["team2"], final["stage_name"]) == (
        "Alpha Squad",
        "Rival Crew",
        "Qualifiers",
    )
    assert (final["score1"], final["score2"], final["status"]) == (5, 3, "done")
    assert final["scheduled_at"] == "2026-11-10T18:00:00Z"
    assert final["mp_match_id"] == 100200300
    assert final["label"] == "Qualifier decider"


def test_a_match_with_no_result_yet_is_scheduled(conn, client_for, clock):
    do_import(conn, client_for, clock)

    open_match = matches(conn)[5001]

    assert open_match["status"] == "scheduled"
    assert open_match["stage_name"] == "Round of 16"
    assert open_match["scheduled_at"] == "2026-12-06T12:00:00Z"
    assert open_match["mp_match_id"] is None


def test_zero_to_zero_is_not_a_result(conn, client_for, clock):
    do_import(conn, client_for, clock)

    assert matches(conn)[5002]["status"] == "scheduled"


def test_an_unscheduled_match_is_stored_without_a_time(conn, client_for, clock):
    do_import(conn, client_for, clock)

    assert matches(conn)[5003]["scheduled_at"] is None


def test_the_sides_keep_their_order(conn, client_for, clock):
    do_import(conn, client_for, clock)

    second = matches(conn)[5002]

    assert (second["team1"], second["team2"]) == ("Third Wheel", "Alpha Squad")


def test_a_solo_match_finds_each_player_by_osu_id(conn, client_for, clock):
    do_import(conn, client_for, clock, "synthetic-solo")

    stored = matches(conn)

    assert (stored[6001]["team1"], stored[6001]["team2"]) == ("AdminAmy", "RivalRex")
    assert (stored[6002]["team1"], stored[6002]["team2"]) == ("CaptainCarl", "RivalRex")
    assert (stored[6002]["score1"], stored[6002]["status"]) == (2, "done")
    assert stored[6002]["mp_match_id"] == 100200301  # a bare number is a lobby id too


def test_a_solo_match_falls_back_to_the_wybin_player_id(conn, fake, client_for, clock):
    data = fake.matches[9002]
    first = data["stages"][0]["matches"][0]
    first["playerOne"] = {"id": 11}  # no osu! profile in it
    first["playerTwo"] = None
    first["opponentTwo"] = 21

    do_import(conn, client_for, clock, "synthetic-solo")

    assert (matches(conn)[6001]["team1"], matches(conn)[6001]["team2"]) == ("AdminAmy", "RivalRex")


def test_a_team_side_that_names_nobody_we_know_stays_empty(conn, fake, client_for, clock):
    fake.matches[9001]["stages"][1]["matches"][0]["opponentTwo"] = {"id": 99999}

    do_import(conn, client_for, clock)

    stored = matches(conn)[5001]
    assert stored["team1"] == "Alpha Squad" and stored["team2"] is None


def test_in_a_team_tournament_a_player_id_is_not_mistaken_for_a_team_id(
    conn, fake, client_for, clock
):
    # Wybin player id 11 is a player; team ids are 801 to 803. A `playerOne` of 801 must not pick
    # the team with wybin id 801 (the two id spaces overlap in principle).
    match = fake.matches[9001]["stages"][1]["matches"][0]
    match["opponentOne"] = None
    match["playerOne"] = {"id": 802}

    do_import(conn, client_for, clock)

    assert matches(conn)[5001]["team1"] is None


def test_a_team_side_given_only_as_a_player_is_matched_through_the_roster(
    conn, fake, client_for, clock
):
    match = fake.matches[9001]["stages"][1]["matches"][0]
    match["opponentTwo"] = {"user": {"userOsu": {"id": 4001}}}  # a Rival Crew player

    do_import(conn, client_for, clock)

    assert matches(conn)[5001]["team2"] == "Rival Crew"


def test_a_stage_wybin_does_not_list_leaves_the_stage_empty(conn, fake, client_for, clock):
    fake.matches[9001]["stages"][0]["id"] = 4242

    do_import(conn, client_for, clock)

    assert matches(conn)[5004]["stage_name"] is None


def test_syncing_again_updates_a_match_instead_of_duplicating_it(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    update = fake.matches[9001]["stages"][1]["matches"][0]
    update.update(date=1796644800000, opponentOneScore=2, opponentTwoScore=5)

    run(sync_tournament(conn, client_for, tid, clock))

    stored = matches(conn)
    assert len(stored) == 4
    assert stored[5001]["scheduled_at"] == "2026-12-07T12:00:00Z"
    assert (stored[5001]["score1"], stored[5001]["score2"], stored[5001]["status"]) == (
        2,
        5,
        "done",
    )


def test_a_rescheduled_match_keeps_its_row(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    before = matches(conn)[5001]["id"]
    fake.matches[9001]["stages"][1]["matches"][0]["date"] = None

    run(sync_tournament(conn, client_for, tid, clock))

    assert matches(conn)[5001]["id"] == before
    assert matches(conn)[5001]["scheduled_at"] is None


def test_a_match_that_disappears_from_wybin_is_kept(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    del fake.matches[9001]["stages"][1]["matches"][2]

    run(sync_tournament(conn, client_for, tid, clock))

    assert 5003 in matches(conn)


def test_a_match_id_belonging_to_another_tournament_is_left_alone(conn, fake, client_for, clock):
    do_import(conn, client_for, clock)
    # The solo tournament's match list reuses wybin match id 5001 with a different label.
    stolen = copy.deepcopy(fake.matches[9001]["stages"][1]["matches"][0])
    stolen["label"] = "stolen"
    fake.matches[9002] = {"id": 9002, "stages": [{"id": 711, "matches": [stolen]}]}

    do_import(conn, client_for, clock, "synthetic-solo")

    rows = conn.execute("SELECT tournament_id, label FROM matches WHERE wybin_match_id = 5001")
    assert [row["label"] for row in rows.fetchall()] == ["R16 M1"]


def test_an_unreadable_matches_endpoint_keeps_the_stored_matches_and_is_reported(
    conn, fake, client_for, clock
):
    tid = do_import(conn, client_for, clock).tournament_id
    fake.matches[9001]["stages"] = "garbage"
    fake.tournaments["synthetic-cup"]["name"] = "Renamed"

    result = run(sync_tournament(conn, client_for, tid, clock))

    assert result.matches_unreadable and not result.pools_unreadable
    assert len(matches(conn)) == 4
    assert (
        conn.execute("SELECT name FROM tournaments WHERE id = ?", (tid,)).fetchone()[0] == "Renamed"
    )


def test_a_missing_matches_endpoint_does_not_stop_the_import(conn, fake, client_for, clock):
    fake.missing_kinds.add("tournament-matches")

    result = do_import(conn, client_for, clock)

    assert result.matches_unreadable
    assert conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 3
    assert matches(conn) == {}


def test_a_readable_matches_endpoint_is_not_reported(conn, client_for, clock):
    assert not do_import(conn, client_for, clock).matches_unreadable


def test_the_sync_job_reports_unreadable_matches(conn, fake, client_for, clock):
    do_import(conn, client_for, clock)
    fake.missing_kinds.add("tournament-matches")

    with pytest.raises(WybinSyncError, match="matches could not be updated for 1"):
        run(sync_active(conn, client_for, clock))


def test_a_sync_that_cannot_reach_wybin_stores_no_matches(conn, fake, client_for, clock):
    fake.down = True

    with pytest.raises(WybinUnavailable):
        do_import(conn, client_for, clock)

    assert matches(conn) == {}


def test_an_archived_tournament_keeps_its_matches_untouched(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    conn.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (tid,))
    fake.matches[9001]["stages"][1]["matches"][0]["opponentOneScore"] = 9

    with pytest.raises(TournamentArchivedError):
        run(sync_tournament(conn, client_for, tid, clock))

    assert matches(conn)[5001]["score1"] is None


# --- what the admin sees -----------------------------------------------------------------------

MATCHES_WARNING = "the matches could not be read"


def test_the_admin_import_warns_when_the_matches_could_not_be_read(client, login, csrf, fake):
    login(ADMIN_ID)
    fake.missing_kinds.add("tournament-matches")

    response = client.post(
        "/admin/tournaments/import",
        data={**csrf(), "link": "synthetic-cup"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert MATCHES_WARNING in page and "alert--warning" in page
    assert "Synthetic Catch Cup imported." not in page
