"""Storing a lobby: pool index, hits, registered players, scores, and reading it twice."""

from datetime import timedelta

import pytest

from app.clients.http import make_http_client
from app.clients.osu import OsuClient, OsuNotFound
from app.clients.ratelimit import Priority
from app.clock import utc_iso_or_none
from app.domain.models import FetchedMatch, MatchSummary
from app.services.lobbies import (
    POOL_INDEX_TTL,
    PoolIndexCache,
    build_pool_index,
    import_lobby,
    import_wybin_links,
    process_match,
)
from tests.support import (
    add_match,
    add_member,
    add_pool_map,
    add_stage,
    add_team,
    add_tournament,
)

pytestmark = pytest.mark.anyio

MP = 120_001
OURS, MATE, THEIRS, STRANGER = 2001, 2002, 3001, 9999
NM1, HD1 = 1001, 1002  # beatmap ids of the pool maps


@pytest.fixture
def cup(db):
    """A running tournament: our team (captain and a mate, both on the roster) against a team
    with one player, and a current stage with two maps."""
    tid = add_tournament(db, "Spring Cup")
    add_member(db, tid, OURS, "captain")
    add_member(db, tid, MATE, "player")
    ours = add_team(db, tid, "Ours", [(OURS, "Captain"), (MATE, "Mate")], ours=True)
    theirs = add_team(db, tid, "Theirs", [(THEIRS, "Rival")])
    stage = add_stage(db, tid)
    maps = {
        "NM1": add_pool_map(db, stage, "NM1", beatmap_id=NM1),
        "HD1": add_pool_map(db, stage, "HD1", beatmap_id=HD1, required_mods="HD"),
    }
    return {"id": tid, "stage": stage, "maps": maps, "ours": ours, "theirs": theirs}


def fetched(osu_match, **summary):
    """What `OsuClient.fetch_match` returns for a lobby of the fake osu!."""
    values = {**osu_match["match"], **summary}
    for key in ("start_time", "end_time"):  # the client reads times into the database format
        values[key] = utc_iso_or_none(values[key])
    return FetchedMatch(MatchSummary(**values), list(osu_match["events"]), False)


def rows(db, sql):
    return [tuple(row) for row in db.execute(sql)]


def process(db, clock, osu, cup=None, discovered_by="scan", **kwargs):
    index = build_pool_index(db, clock.now())
    return process_match(db, index, fetched(osu.matches[MP]), discovered_by, clock.now(), **kwargs)


# --- the pool index ---


def test_the_index_holds_released_current_maps_of_active_tournaments(db, clock, cup):
    stage = add_stage(db, cup["id"], "Quarterfinals", sort_order=2)
    add_pool_map(db, stage, "NM1", beatmap_id=1500)
    unreleased = add_stage(db, cup["id"], "Semifinals", sort_order=3, released=False)
    add_pool_map(db, unreleased, "NM1", beatmap_id=1600)
    ended = add_stage(db, cup["id"], "Round of 32", sort_order=0)
    add_pool_map(db, ended, "NM1", beatmap_id=1700)
    db.execute("UPDATE stages SET end_at = '2026-11-01T00:00:00Z' WHERE id = ?", (ended,))
    add_pool_map(db, stage, "NM2", beatmap_id=None)
    db.execute("UPDATE pool_maps SET beatmap_id = NULL WHERE slot = 'NM2'")

    index = build_pool_index(db, clock.now())

    assert sorted(index.by_beatmap) == [NM1, HD1, 1500]


def test_a_stage_ending_in_the_future_or_without_an_end_is_current(db, clock, cup):
    later = add_stage(db, cup["id"], "Finals", sort_order=2)
    add_pool_map(db, later, "NM1", beatmap_id=1800)
    db.execute("UPDATE stages SET end_at = '2026-11-20T00:00:00Z' WHERE id = ?", (later,))

    assert 1800 in build_pool_index(db, clock.now()).by_beatmap


@pytest.mark.parametrize("status", ["archived", "hidden"])
def test_archived_and_hidden_tournaments_are_not_scanned_for(db, clock, cup, status):
    db.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, cup["id"]))

    assert build_pool_index(db, clock.now()).by_beatmap == {}


def test_one_beatmap_in_two_tournaments_is_found_for_both(db, clock, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    other_stage = add_stage(db, other)
    add_pool_map(db, other_stage, "NM1", beatmap_id=NM1)

    entries = build_pool_index(db, clock.now()).by_beatmap[NM1]

    assert {entry.tournament_id for entry in entries} == {cup["id"], other}


def test_an_index_for_one_tournament_also_holds_its_finished_stages(db, clock, cup):
    ended = add_stage(db, cup["id"], "Round of 32", sort_order=0)
    add_pool_map(db, ended, "NM1", beatmap_id=1700)
    db.execute("UPDATE stages SET end_at = '2026-11-01T00:00:00Z' WHERE id = ?", (ended,))

    current = build_pool_index(db, clock.now())
    whole = build_pool_index(db, clock.now(), cup["id"], include_ended=True)

    assert 1700 not in current.by_beatmap
    assert 1700 in whole.by_beatmap


def test_the_cached_index_is_rebuilt_after_five_minutes_or_when_invalidated(db, clock, cup):
    cache = PoolIndexCache()
    assert sorted(cache.get(db, clock.now()).by_beatmap) == [NM1, HD1]
    add_pool_map(db, cup["stage"], "NM2", beatmap_id=1900)

    assert 1900 not in cache.get(db, clock.now()).by_beatmap  # still the cached one
    assert 1900 not in cache.get(db, clock.now() + POOL_INDEX_TTL - timedelta(seconds=1)).by_beatmap
    assert 1900 in cache.get(db, clock.now() + POOL_INDEX_TTL).by_beatmap

    add_pool_map(db, cup["stage"], "NM3", beatmap_id=1901)
    cache.invalidate()
    assert 1901 in cache.get(db, clock.now() + POOL_INDEX_TTL).by_beatmap


# --- a lobby with a pool map, whatever its name ---


def test_a_lobby_called_just_practice_that_played_a_pool_map_is_stored(db, clock, osu, cup):
    osu.add_match(MP, "just practice")
    osu.add_game(MP, NM1, [(OURS, 900_000), (MATE, 850_000)])

    result = process(db, clock, osu)

    assert (result.hit_games, result.stored, result.new_scores) == (1, True, 2)
    assert rows(db, "SELECT mp_match_id, name, discovered_by, is_complete FROM lobbies") == [
        (MP, "just practice", "scan", 1)
    ]
    assert rows(db, "SELECT mp_match_id, pool_map_id FROM lobby_pool_hits") == [
        (MP, cup["maps"]["NM1"])
    ]


def test_a_lobby_without_a_pool_map_stores_nothing(db, clock, osu, cup):
    osu.add_match(MP, "ranked fun")
    osu.add_game(MP, 424242, [(OURS, 900_000)])

    result = process(db, clock, osu)

    assert (result.hit_games, result.stored) == (0, False)
    for table in ("lobbies", "lobby_pool_hits", "lobby_players", "scores"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_a_game_in_another_mode_is_not_a_hit(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000)], mode_int=3)

    assert process(db, clock, osu).stored is False


def test_a_game_still_being_played_is_not_a_hit_yet(db, clock, osu, cup):
    osu.add_match(MP, end_time=None)
    osu.add_game(MP, NM1, ended=False)

    assert process(db, clock, osu).stored is False


# --- players and scores ---


def test_the_score_is_stored_as_a_multiplayer_score(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, HD1, [(OURS, 900_000)], scoring_type="scorev2", score_mods=["HD", "NF"])

    process(db, clock, osu)

    (row,) = rows(
        db,
        "SELECT beatmap_id, osu_id, gamemode, mods, score, scoring, accuracy, max_combo, misses, "
        "passed, achieved_at, source, source_url, entered_by, tournament_id, created_at "
        "FROM scores",
    )
    assert row == (
        HD1, OURS, 0, "HD", 900_000, "v2", 98.0, 400, 2, 1,
        "2026-11-12T10:14:00Z", "mp", f"https://osu.ppy.sh/community/matches/{MP}",
        None, None, "2026-11-12T12:30:15Z",
    )  # fmt: skip
    (ref,) = rows(db, "SELECT source_ref FROM scores")
    assert ref[0].startswith(f"{MP}:")


def test_the_scoring_of_the_room_is_kept_and_a_failed_score_is_marked(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 500_000)], scoring_type="score")
    event = osu.add_game(MP, NM1, [(MATE, 400_000)], scoring_type="accuracy")
    event["game"]["scores"][0]["match"]["pass"] = False

    process(db, clock, osu)

    assert rows(db, "SELECT osu_id, scoring, passed FROM scores ORDER BY osu_id") == [
        (OURS, "v1", 1),
        (MATE, "unknown", 0),
    ]


def test_only_players_registered_in_the_tournament_are_noted(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000), (STRANGER, 950_000)])

    process(db, clock, osu)

    assert rows(db, "SELECT osu_id FROM lobby_players") == [(OURS,)]
    assert rows(db, "SELECT osu_id FROM scores") == [(OURS,)]


def test_players_of_other_teams_are_registered_players_too(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000), (THEIRS, 880_000)])

    process(db, clock, osu)

    assert rows(db, "SELECT osu_id, team_id, tournament_id FROM lobby_players ORDER BY osu_id") == [
        (OURS, cup["ours"], cup["id"]),
        (THEIRS, cup["theirs"], cup["id"]),
    ]
    assert rows(db, "SELECT osu_id FROM scores ORDER BY osu_id") == [(OURS,), (THEIRS,)]


def test_a_roster_member_without_a_team_is_registered_without_one(db, clock, osu, cup):
    add_member(db, cup["id"], 2003, "player")
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(2003, 700_000)])

    process(db, clock, osu)

    assert rows(db, "SELECT osu_id, team_id FROM lobby_players") == [(2003, None)]


def test_a_beatmap_in_two_tournaments_notes_each_registered_player_once_per_tournament(
    db, clock, osu, cup
):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, OURS, "player")
    add_pool_map(db, add_stage(db, other), "NM1", beatmap_id=NM1)
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000)])

    process(db, clock, osu)

    assert rows(db, "SELECT tournament_id FROM lobby_players ORDER BY tournament_id") == [
        (cup["id"],),
        (other,),
    ]
    assert db.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1  # the score is public


def test_the_players_scores_count_on_the_pool_sheet(db, clock, osu, cup, client, login):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu)
    login(OURS)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "900,000" in page


# --- reading twice ---


def test_the_same_lobby_read_twice_is_stored_once(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000), (THEIRS, 800_000)])

    first = process(db, clock, osu)
    second = process(db, clock, osu)

    assert (first.new_scores, second.new_scores) == (2, 0)
    for table, expected in (("lobbies", 1), ("lobby_pool_hits", 1), ("lobby_players", 2)):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected  # noqa: S608
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2


async def test_a_scan_and_a_pasted_link_of_one_lobby_make_one_lobby(
    db, clock, osu, cup, scan_client
):
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu)

    await import_lobby(db, scan_client, MP, cup["id"], "manual", clock.now())

    assert rows(db, "SELECT discovered_by FROM lobbies") == [("scan",)]
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM lobby_players").fetchone()[0] == 1


def test_a_complete_lobby_stays_complete_and_an_open_one_is_finished_by_the_last_read(
    db, clock, osu, cup
):
    osu.add_match(MP, end_time=None)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu)
    assert rows(db, "SELECT is_complete, ended_at FROM lobbies") == [(0, None)]

    process(db, clock, osu, final=True)
    assert rows(db, "SELECT is_complete FROM lobbies") == [(1,)]

    process(db, clock, osu)  # a read that says "open" does not reopen it
    assert rows(db, "SELECT is_complete FROM lobbies") == [(1,)]


def test_a_later_read_refreshes_the_name_and_end_but_keeps_how_it_was_found(db, clock, osu, cup):
    osu.add_match(MP, "room", end_time=None)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu, discovered_by="wybin")
    osu.matches[MP]["match"].update(name="renamed", end_time="2026-11-12T11:30:00+00:00")

    clock.current += timedelta(hours=1)
    process(db, clock, osu, discovered_by="scan")

    assert rows(
        db, "SELECT name, ended_at, discovered_by, first_seen_at, last_scanned_at FROM lobbies"
    ) == [
        ("renamed", "2026-11-12T11:30:00Z", "wybin", "2026-11-12T12:30:15Z", "2026-11-12T13:30:15Z")
    ]


# --- links from wybin and members ---


async def test_a_pasted_lobby_is_stored_even_without_a_pool_map(db, clock, osu, cup, scan_client):
    osu.add_match(MP, "old practice")
    osu.add_game(MP, 424242, [(OURS, 900_000), (STRANGER, 1)])

    result = await import_lobby(db, scan_client, MP, cup["id"], "manual", clock.now())

    assert (result.hit_games, result.stored) == (0, True)
    assert rows(db, "SELECT discovered_by FROM lobbies") == [("manual",)]
    assert rows(db, "SELECT osu_id FROM lobby_players") == [(OURS,)]
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


async def test_a_pasted_lobby_finds_pool_maps_of_a_finished_stage(db, clock, osu, cup, scan_client):
    db.execute("UPDATE stages SET end_at = '2026-11-01T00:00:00Z' WHERE id = ?", (cup["stage"],))
    osu.add_match(MP)
    osu.add_game(MP, NM1, [(OURS, 900_000)])

    result = await import_lobby(db, scan_client, MP, cup["id"], "manual", clock.now())

    assert result.hit_games == 1
    assert db.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 1


async def test_a_lobby_osu_does_not_have_raises_and_stores_nothing(
    db, clock, osu, cup, scan_client
):
    with pytest.raises(OsuNotFound):
        await import_lobby(db, scan_client, MP, cup["id"], "manual", clock.now())

    assert db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0] == 0


def link(db, cup, mp_id, scheduled_at="2026-11-10T18:00:00Z"):
    """A match of the tournament whose wybin lobby link names this lobby."""
    match_id = add_match(db, cup["id"], cup["ours"], cup["theirs"], scheduled_at, cup["stage"])
    db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (mp_id, match_id))


async def test_lobbies_linked_on_wybin_are_read_once_they_are_finished(
    db, clock, osu, cup, scan_client
):
    for number, mp_id in enumerate((MP, MP + 1, MP + 2)):
        osu.add_match(mp_id)
        osu.add_game(mp_id, NM1 if number else 424242, [(OURS, 900_000 + number)])
        link(db, cup, mp_id, f"2026-11-1{number}T18:00:00Z")

    first = await import_wybin_links(db, scan_client, clock.now())
    second = await import_wybin_links(db, scan_client, clock.now())

    assert (first, second) == (3, 0)
    assert rows(db, "SELECT mp_match_id, discovered_by FROM lobbies ORDER BY 1") == [
        (MP, "wybin"),
        (MP + 1, "wybin"),
        (MP + 2, "wybin"),
    ]


async def test_wybin_links_read_the_newest_first_and_a_few_at_a_time(
    db, clock, osu, cup, scan_client
):
    for number in range(5):
        mp_id = MP + number
        osu.add_match(mp_id)
        link(db, cup, mp_id, f"2026-11-0{number + 1}T18:00:00Z")

    read = await import_wybin_links(db, scan_client, clock.now(), limit=2)

    assert read == 2
    assert rows(db, "SELECT mp_match_id FROM lobbies ORDER BY 1") == [(MP + 3,), (MP + 4,)]


async def test_an_unfinished_wybin_lobby_is_read_again_and_a_dead_link_is_skipped(
    db, clock, osu, cup, scan_client
):
    osu.add_match(MP, end_time=None)
    link(db, cup, MP)
    link(db, cup, MP + 1, "2026-11-11T18:00:00Z")  # osu! has no such lobby

    assert await import_wybin_links(db, scan_client, clock.now()) == 1
    assert rows(db, "SELECT is_complete FROM lobbies") == [(0,)]
    osu.matches[MP]["match"]["end_time"] = "2026-11-12T11:00:00+00:00"

    assert await import_wybin_links(db, scan_client, clock.now()) == 1
    assert rows(db, "SELECT is_complete FROM lobbies") == [(1,)]
    assert await import_wybin_links(db, scan_client, clock.now()) == 0


async def test_wybin_links_of_archived_tournaments_are_not_read(db, clock, osu, cup, scan_client):
    osu.add_match(MP)
    link(db, cup, MP)
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))

    assert await import_wybin_links(db, scan_client, clock.now()) == 0
    assert osu.match_requests(MP) == []


async def test_pasted_and_wybin_lobbies_are_requested_at_interactive_priority(
    db, clock, osu, cup, settings
):
    class Recording:
        def __init__(self):
            self.priorities = []

        async def acquire(self, priority):
            self.priorities.append(priority)

    limiter = Recording()
    client = OsuClient(
        settings, make_http_client(osu.transport), limiter, clock, transport=osu.transport
    )
    osu.add_match(MP)
    osu.add_match(MP + 1)
    link(db, cup, MP + 1)

    await import_lobby(db, client, MP, cup["id"], "manual", clock.now())
    await import_wybin_links(db, client, clock.now())

    assert set(limiter.priorities) == {Priority.INTERACTIVE}
    assert len(limiter.priorities) >= 3  # the token, then one request per lobby


# --- a stored lobby is finished by a read without a new pool game ---


def test_a_stored_lobby_is_completed_by_a_read_that_only_has_other_games(db, clock, osu, cup):
    osu.add_match(MP, "room", end_time=None)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu)
    assert rows(db, "SELECT is_complete, ended_at FROM lobbies") == [(0, None)]

    osu.add_game(MP, 424242, [(OURS, 1)])  # the lobby ends after a game on another map
    osu.matches[MP]["match"].update(name="renamed", end_time="2026-11-12T11:30:00+00:00")
    clock.current += timedelta(hours=1)
    result = process(db, clock, osu)

    assert (result.hit_games, result.stored) == (1, True)  # the earlier pool game is still there
    assert rows(db, "SELECT name, is_complete, ended_at, last_scanned_at FROM lobbies") == [
        ("renamed", 1, "2026-11-12T11:30:00Z", "2026-11-12T13:30:15Z")
    ]


def test_a_closing_read_with_no_pool_game_at_all_still_finishes_a_stored_lobby(db, clock, osu, cup):
    osu.add_match(MP, "room", end_time=None)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu)
    osu.matches[MP]["events"].clear()  # a read with no events (only the newer ones are asked for)
    osu.matches[MP]["match"]["end_time"] = "2026-11-12T11:30:00+00:00"

    result = process(db, clock, osu)

    assert (result.hit_games, result.stored) == (0, True)
    assert rows(db, "SELECT is_complete, ended_at FROM lobbies") == [(1, "2026-11-12T11:30:00Z")]
    assert db.execute("SELECT COUNT(*) FROM lobby_pool_hits").fetchone()[0] == 1  # kept


def test_a_final_read_completes_a_stored_lobby_that_osu_still_calls_open(db, clock, osu, cup):
    osu.add_match(MP, end_time=None)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    process(db, clock, osu)
    osu.matches[MP]["events"].clear()

    process(db, clock, osu, final=True)

    assert rows(db, "SELECT is_complete FROM lobbies") == [(1,)]


def test_a_read_of_a_lobby_that_is_not_stored_stores_nothing(db, clock, osu, cup):
    osu.add_match(MP)
    osu.add_game(MP, 424242, [(OURS, 1)])

    result = process(db, clock, osu)

    assert (result.stored, db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0]) == (False, 0)


async def test_a_finished_wybin_lobby_is_not_read_again_after_a_closing_read_without_hits(
    db, clock, osu, cup, scan_client
):
    osu.add_match(MP, end_time=None)
    osu.add_game(MP, NM1, [(OURS, 900_000)])
    link(db, cup, MP)
    assert await import_wybin_links(db, scan_client, clock.now()) == 1
    osu.add_game(MP, 424242, [(OURS, 1)])
    osu.matches[MP]["match"]["end_time"] = "2026-11-12T11:30:00+00:00"
    process(db, clock, osu)  # the scanner's closing read, nothing new on pool maps

    assert await import_wybin_links(db, scan_client, clock.now()) == 0


# --- wybin links osu! does not have are given up on ---


async def test_a_wybin_link_osu_does_not_have_is_tried_three_times_and_then_left_alone(
    db, clock, osu, cup, scan_client
):
    link(db, cup, MP)

    reads = [await import_wybin_links(db, scan_client, clock.now()) for _ in range(5)]

    assert reads == [0] * 5
    assert len(osu.match_requests(MP)) == 3


async def test_a_given_up_link_no_longer_takes_one_of_the_slots_of_a_run(
    db, clock, osu, cup, scan_client
):
    link(db, cup, MP, "2026-11-12T09:00:00Z")  # the newest, and osu! has no such lobby
    for number in (1, 2):
        osu.add_match(MP + number)
        link(db, cup, MP + number, f"2026-11-0{number}T18:00:00Z")
    for _ in range(3):
        await import_wybin_links(db, scan_client, clock.now(), limit=1)
    assert rows(db, "SELECT COUNT(*) FROM lobbies") == [(0,)]

    assert await import_wybin_links(db, scan_client, clock.now(), limit=1) == 1
    assert rows(db, "SELECT mp_match_id FROM lobbies") == [(MP + 2,)]  # the newest of the rest
