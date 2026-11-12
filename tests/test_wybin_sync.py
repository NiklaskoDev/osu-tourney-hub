import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.clients.http import make_http_client
from app.clients.wybin import InvalidWybinLink, WybinClient
from app.clients.wybin_models import (
    WybinBeatmap,
    WybinBracket,
    WybinFormatError,
    WybinNotFound,
    WybinUnavailable,
)
from app.repos import deleted_tournaments as deleted_repo
from app.repos import wybin_links as links_repo
from app.services import archive as archive_service
from app.services.wybin_sync import (
    TournamentArchivedError,
    WybinSyncError,
    build_pool,
    import_tournament,
    sync_active,
    sync_tournament,
    track_signups,
)
from tests.support import ADMIN_ID, add_member, add_tournament, add_user, load_fixture

TABLES = ("tournaments", "stages", "pool_maps", "teams", "team_players", "tournament_members")


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
    """The test database with the admin known, as after an osu! login."""
    add_user(db, ADMIN_ID, "AdminAmy")
    return db


def run(coroutine):
    return asyncio.run(coroutine)


def counts(db) -> dict[str, int]:
    return {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in TABLES
    }


def do_import(conn, client, clock, text="synthetic-cup", importer=ADMIN_ID, **kwargs):
    return run(import_tournament(conn, client, text, importer, clock, **kwargs))


def tournament_row(conn, wybin_id=9001):
    return conn.execute("SELECT * FROM tournaments WHERE wybin_id = ?", (wybin_id,)).fetchone()


def members(conn, tid):
    rows = conn.execute(
        "SELECT osu_id, role, source, is_substitute FROM tournament_members "
        "WHERE tournament_id = ? ORDER BY osu_id",
        (tid,),
    ).fetchall()
    return {row["osu_id"]: (row["role"], row["source"]) for row in rows}


def slots(conn, stage_name, tid):
    rows = conn.execute(
        "SELECT p.* FROM pool_maps p JOIN stages s ON s.id = p.stage_id "
        "WHERE s.tournament_id = ? AND s.name = ? ORDER BY p.sort_order",
        (tid, stage_name),
    ).fetchall()
    return {row["slot"]: row for row in rows}


# --- import: tournament, stages, pool ---


def test_import_creates_the_tournament_from_wybin(conn, client_for, clock):
    result = do_import(conn, client_for, clock)

    row = tournament_row(conn)
    assert result.created and result.tournament_id == row["id"]
    assert (row["name"], row["acronym"], row["slug"]) == (
        "Synthetic Catch Cup",
        "SCC",
        "synthetic-cup",
    )
    assert (row["gamemode"], row["is_solo"]) == (2, 0)
    assert (row["team_size_min"], row["team_size_max"]) == (3, 6)
    assert (row["source"], row["status"], row["owner_osu_id"]) == ("wybin", "active", ADMIN_ID)
    assert row["last_synced_at"] == "2026-11-12T12:30:15Z"
    assert (row["freemod_rule"], row["scoring"]) == ("none", "v2")


def test_import_accepts_a_wybin_address(conn, client_for, clock):
    do_import(conn, client_for, clock, "https://wybin.xyz/synthetic-cup/home")

    assert tournament_row(conn) is not None


def test_stages_are_stored_in_date_order_with_their_format(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id

    rows = conn.execute(
        "SELECT name, sort_order, start_at, end_at, best_of, bans, protects, is_qualifier, "
        "lineup_size, pool_released FROM stages WHERE tournament_id = ? ORDER BY sort_order",
        (tid,),
    ).fetchall()

    assert [tuple(row)[:2] for row in rows] == [
        ("Qualifiers", 1),
        ("Round of 16", 2),
        ("Finals", 3),
    ]
    assert rows[0]["start_at"] == "2026-11-20T12:00:00Z"
    assert rows[0]["is_qualifier"] == 1 and rows[1]["is_qualifier"] == 0
    assert tuple(rows[1])[4:7] == (9, 1, None)
    assert tuple(rows[2])[4:7] == (13, 2, 1)
    assert all(row["lineup_size"] is None for row in rows)


def test_the_pool_gets_slots_mods_and_flags(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id

    pool = slots(conn, "Qualifiers", tid)

    assert list(pool) == ["NM1", "NM2", "HD1", "HD2", "HR1", "DT1", "FM1", "TB"]
    assert pool["NM1"]["beatmap_id"] == 910001  # wybin listed it second but numbered it first
    assert pool["NM2"]["beatmap_id"] == 910002
    assert {slot: pool[slot]["required_mods"] for slot in pool} == {
        "NM1": "",
        "NM2": "",
        "HD1": "HD",
        "HD2": "HD",
        "HR1": "HR",
        "DT1": "DT",
        "FM1": "",
        "TB": "",
    }
    assert (pool["FM1"]["is_freemod"], pool["FM1"]["is_tiebreaker"]) == (1, 0)
    assert (pool["TB"]["is_freemod"], pool["TB"]["is_tiebreaker"]) == (0, 1)
    assert pool["HD1"]["bracket"] == "Hidden"
    assert (pool["NM1"]["artist"], pool["NM1"]["title"], pool["NM1"]["version"]) == (
        "Synth Artist",
        "First Song",
        "Rain",
    )
    assert pool["NM1"]["mapper"] == "MapperMia"
    assert pool["NM1"]["star_rating"] == 6.5 and pool["NM1"]["length_s"] == 180


def test_pool_released_means_at_least_one_real_map(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id

    released = dict(
        conn.execute(
            "SELECT name, pool_released FROM stages WHERE tournament_id = ?", (tid,)
        ).fetchall()
    )

    assert released == {
        "Qualifiers": 1,
        "Round of 16": 0,
        "Finals": 0,
    }  # only placeholders / no pool
    assert slots(conn, "Round of 16", tid) == {}


def test_inline_pools_are_used_when_the_mappool_endpoint_is_missing(conn, fake, client_for, clock):
    fake.mappools.clear()
    inline = load_fixture("mappools")["stages"][0]["modBrackets"][:1]
    fake.tournaments["synthetic-cup"]["stages"][1]["modBrackets"] = inline  # "Qualifiers" (id 701)

    tid = do_import(conn, client_for, clock).tournament_id

    assert list(slots(conn, "Qualifiers", tid)) == ["NM1", "NM2"]


def test_an_unusable_mappool_endpoint_does_not_stop_the_import(conn, fake, client_for, clock):
    fake.mappools.clear()  # 404 for this tournament

    tid = do_import(conn, client_for, clock).tournament_id

    assert slots(conn, "Qualifiers", tid) == {}
    assert conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 3


def test_a_stage_with_an_unknown_pool_keeps_its_stored_maps(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    fake.mappools.clear()

    run(sync_tournament(conn, client_for, tid, clock))

    assert len(slots(conn, "Qualifiers", tid)) == 8
    assert (
        conn.execute("SELECT pool_released FROM stages WHERE name = 'Qualifiers'").fetchone()[0]
        == 1
    )


def test_a_changed_pool_updates_in_place_and_drops_removed_slots(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    before = slots(conn, "Qualifiers", tid)["HD1"]["id"]
    brackets = fake.mappools[9001]["stages"][0]["modBrackets"]
    brackets[1]["beatmaps"][1]["beatmapId"] = 919999  # HD1 swapped for another map
    del brackets[5]  # tiebreaker removed

    run(sync_tournament(conn, client_for, tid, clock))

    pool = slots(conn, "Qualifiers", tid)
    assert pool["HD1"]["beatmap_id"] == 919999
    assert pool["HD1"]["id"] == before  # same row, so notes and scores attached to it survive
    assert "TB" not in pool


# --- import: teams, our team, members ---


def test_all_teams_and_players_are_stored_including_opponents(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id

    teams = conn.execute(
        "SELECT name, wybin_team_id, seed, is_ours FROM teams WHERE tournament_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    assert [tuple(team) for team in teams] == [
        ("Alpha Squad", 801, 3, 1),
        ("Rival Crew", 802, 1, 0),
        ("Third Wheel", 803, None, 0),
    ]
    rival = conn.execute(
        "SELECT osu_id, username, is_captain FROM team_players "
        "WHERE team_id = (SELECT id FROM teams WHERE name = 'Rival Crew') ORDER BY osu_id"
    ).fetchall()
    assert [tuple(player) for player in rival] == [
        (4001, "RivalRex", 1),
        (4002, "RivalRia", 0),
        (4003, "RivalRob", 0),
    ]


def test_wybin_timezones_are_stored_as_iana_name_and_offset(conn, client_for, clock):
    do_import(conn, client_for, clock)

    rows = {
        row["osu_id"]: (row["timezone_iana"], row["timezone_offset_min"])
        for row in conn.execute("SELECT * FROM team_players")
    }

    assert rows[1001] == ("Europe/Berlin", 60)
    assert rows[3001] == (None, -360)
    assert rows[3002] == (None, 330)


def test_our_team_is_the_one_holding_the_importing_admin(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id

    ours = conn.execute("SELECT id, name FROM teams WHERE is_ours = 1").fetchall()

    assert [row["name"] for row in ours] == ["Alpha Squad"]
    assert tournament_row(conn)["our_team_id"] == ours[0]["id"]
    assert tid


def test_another_importer_gets_their_own_team_as_ours(conn, client_for, clock):
    add_user(conn, 4001, "RivalRex")
    do_import(conn, client_for, clock, importer=4001)

    assert conn.execute("SELECT name FROM teams WHERE is_ours = 1").fetchone()[0] == "Rival Crew"


def test_the_roster_of_our_team_becomes_members_with_the_captain_flags(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id

    assert members(conn, tid) == {
        1001: ("player", "wybin"),
        3001: ("captain", "wybin"),  # team creator
        3002: ("player", "wybin"),
        3003: ("captain", "wybin"),  # temporary team captain
    }


def test_roster_members_get_a_name_for_the_members_page(conn, client_for, clock):
    do_import(conn, client_for, clock)

    row = conn.execute(
        "SELECT username, avatar_url, country FROM users WHERE osu_id = 3002"
    ).fetchone()

    assert tuple(row) == ("PlayerPia", "https://a.ppy.sh/3002?1", "DE")


def test_a_logged_in_users_profile_is_not_overwritten_by_wybin(conn, client_for, clock):
    add_user(conn, 3002, "RenamedPia")

    do_import(conn, client_for, clock)

    assert (
        conn.execute("SELECT username FROM users WHERE osu_id = 3002").fetchone()[0] == "RenamedPia"
    )


def test_no_team_for_the_admin_means_no_our_team_and_no_members(conn, fake, client_for, clock):
    for team in fake.tournaments["synthetic-cup"]["teams"]:
        team["teamMembers"] = [m for m in team["teamMembers"] if m["user"]["userOsu"]["id"] != 1001]

    tid = do_import(conn, client_for, clock).tournament_id

    assert tournament_row(conn)["our_team_id"] is None
    assert members(conn, tid) == {}
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 3


def test_a_later_sync_finds_our_team_once_the_admin_is_on_one(conn, fake, client_for, clock):
    teams = fake.tournaments["synthetic-cup"]["teams"]
    amy = teams[0]["teamMembers"].pop(0)
    tid = do_import(conn, client_for, clock).tournament_id
    assert tournament_row(conn)["our_team_id"] is None
    teams[0]["teamMembers"].insert(0, amy)

    run(sync_tournament(conn, client_for, tid, clock))

    assert tournament_row(conn)["our_team_id"] is not None
    assert members(conn, tid)[3001] == ("captain", "wybin")  # first assignment hands out captains


def test_a_linked_admin_counts_when_the_importer_is_not_on_a_team(conn, client_for, clock):
    add_user(conn, 4002, "RivalRia")
    add_user(conn, 9999, "OtherAdmin")
    links_repo.upsert(conn, 4002, 555)  # a tracked admin who is on Rival Crew

    do_import(conn, client_for, clock, importer=9999)

    assert conn.execute("SELECT name FROM teams WHERE is_ours = 1").fetchone()[0] == "Rival Crew"


# --- re-sync ---


def test_a_second_sync_changes_nothing(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    before = counts(conn)
    stages_before = [tuple(r) for r in conn.execute("SELECT * FROM stages ORDER BY id")]
    maps_before = [tuple(r) for r in conn.execute("SELECT * FROM pool_maps ORDER BY id")]
    members_before = members(conn, tid)

    result = run(sync_tournament(conn, client_for, tid, clock))

    assert not result.created and result.tournament_id == tid
    assert counts(conn) == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM stages ORDER BY id")] == stages_before
    assert [tuple(r) for r in conn.execute("SELECT * FROM pool_maps ORDER BY id")] == maps_before
    assert members(conn, tid) == members_before


def test_importing_a_known_tournament_again_refreshes_it(conn, client_for, clock):
    first = do_import(conn, client_for, clock)

    second = do_import(conn, client_for, clock)

    assert not second.created and second.tournament_id == first.tournament_id
    assert counts(conn)["tournaments"] == 1


def test_a_sync_updates_what_wybin_owns(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    payload = fake.tournaments["synthetic-cup"]
    payload["name"] = "Renamed Cup"
    payload["stages"][1]["name"] = "Qualification"
    payload["teams"][1]["name"] = "Rival Crew Reborn"
    clock.current += timedelta(hours=1)

    run(sync_tournament(conn, client_for, tid, clock))

    assert tournament_row(conn)["name"] == "Renamed Cup"
    assert tournament_row(conn)["last_synced_at"] == "2026-11-12T13:30:15Z"
    assert (
        conn.execute("SELECT name FROM stages WHERE wybin_stage_id = 701").fetchone()[0]
        == "Qualification"
    )
    assert (
        conn.execute("SELECT name FROM teams WHERE wybin_team_id = 802").fetchone()[0]
        == "Rival Crew Reborn"
    )


def test_a_sync_never_touches_the_apps_own_settings(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    conn.execute(
        "UPDATE tournaments SET freemod_rule = 'two_distinct_mods', scoring = 'lazer', "
        "final_placing = 'Top 8', status = 'hidden' WHERE id = ?",
        (tid,),
    )
    conn.execute("UPDATE stages SET lineup_size = 4 WHERE tournament_id = ?", (tid,))

    run(sync_tournament(conn, client_for, tid, clock))

    row = tournament_row(conn)
    assert (row["freemod_rule"], row["scoring"], row["final_placing"], row["status"]) == (
        "two_distinct_mods",
        "lazer",
        "Top 8",
        "hidden",
    )
    assert {r[0] for r in conn.execute("SELECT lineup_size FROM stages")} == {4}


def test_manual_members_and_edits_survive_a_sync(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    add_member(conn, tid, 7001, "guest")
    conn.execute(
        "UPDATE tournament_members SET role = 'player', is_substitute = 1 "
        "WHERE tournament_id = ? AND osu_id = 3002",
        (tid,),
    )
    conn.execute(
        "UPDATE tournament_members SET role = 'co_captain' "
        "WHERE tournament_id = ? AND osu_id = 3003",
        (tid,),
    )

    run(sync_tournament(conn, client_for, tid, clock))

    found = members(conn, tid)
    assert found[7001] == ("guest", "manual")
    assert found[3003] == ("co_captain", "wybin")
    assert (
        conn.execute(
            "SELECT is_substitute FROM tournament_members "
            "WHERE osu_id = 3002 AND tournament_id = ?",
            (tid,),
        ).fetchone()[0]
        == 1
    )


def test_a_sync_removes_nobody(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    fake.tournaments["synthetic-cup"]["teams"][0]["teamMembers"].pop()  # PlayerPaul left the team

    run(sync_tournament(conn, client_for, tid, clock))

    assert 3003 in members(conn, tid)
    assert conn.execute("SELECT COUNT(*) FROM team_players WHERE osu_id = 3003").fetchone()[0] == 0


def test_a_new_roster_player_joins_as_a_plain_player_never_as_captain(
    conn, fake, client_for, clock
):
    tid = do_import(conn, client_for, clock).tournament_id
    newcomer = load_fixture("tournament_team")["players"][-1]  # LoneLou, flagged as captain below
    newcomer["temporaryTeamCaptain"] = True
    fake.tournaments["synthetic-cup"]["teams"][0]["teamMembers"].append(newcomer)

    run(sync_tournament(conn, client_for, tid, clock))

    assert members(conn, tid)[6001] == ("player", "wybin")


def test_a_changed_wybin_captain_does_not_change_roles_after_the_first_sync(
    conn, fake, client_for, clock
):
    tid = do_import(conn, client_for, clock).tournament_id
    team = fake.tournaments["synthetic-cup"]["teams"][0]
    team["creator"] = team["teamMembers"][2]  # PlayerPia now created the team

    run(sync_tournament(conn, client_for, tid, clock))

    assert members(conn, tid)[3001][0] == "captain"
    assert members(conn, tid)[3002][0] == "player"
    assert (
        conn.execute(
            "SELECT is_captain FROM team_players WHERE osu_id = 3002 AND team_id = ?",
            (tournament_row(conn)["our_team_id"],),
        ).fetchone()[0]
        == 1
    )  # the team data itself follows wybin


def test_a_manually_chosen_our_team_is_kept(conn, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    other = conn.execute("SELECT id FROM teams WHERE name = 'Rival Crew'").fetchone()[0]
    conn.execute("UPDATE tournaments SET our_team_id = ? WHERE id = ?", (other, tid))
    conn.execute("UPDATE teams SET is_ours = (id = ?) WHERE tournament_id = ?", (other, tid))

    run(sync_tournament(conn, client_for, tid, clock))

    assert tournament_row(conn)["our_team_id"] == other


# --- failures leave data unchanged ---


def test_when_wybin_is_down_the_sync_fails_and_data_stays(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    snapshot = counts(conn), tuple(tournament_row(conn))
    fake.tournaments["synthetic-cup"]["name"] = "Changed"
    fake.down = True

    with pytest.raises(WybinUnavailable):
        run(sync_tournament(conn, client_for, tid, clock))

    assert (counts(conn), tuple(tournament_row(conn))) == snapshot


def test_an_unreadable_answer_fails_the_sync_and_data_stays(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    snapshot = counts(conn), tuple(tournament_row(conn))
    fake.tournaments["synthetic-cup"]["stages"] = "broken"

    with pytest.raises(WybinFormatError):
        run(sync_tournament(conn, client_for, tid, clock))

    assert (counts(conn), tuple(tournament_row(conn))) == snapshot


def test_a_failed_first_import_creates_nothing(conn, fake, client_for, clock):
    fake.down = True

    with pytest.raises(WybinUnavailable):
        do_import(conn, client_for, clock)

    assert counts(conn) == dict.fromkeys(TABLES, 0)


def test_a_database_error_halfway_rolls_the_whole_sync_back(conn, client_for, clock, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr("app.repos.teams.replace_players", fail)  # after tournament and stages

    with pytest.raises(sqlite3.OperationalError):
        do_import(conn, client_for, clock)

    assert counts(conn) == dict.fromkeys(TABLES, 0)
    assert not conn.in_transaction


def test_unknown_and_invalid_tournaments_are_reported(conn, client_for, clock):
    with pytest.raises(WybinNotFound):
        do_import(conn, client_for, clock, "does-not-exist")
    with pytest.raises(InvalidWybinLink):
        do_import(conn, client_for, clock, "https://evil.example/x")


def test_only_wybin_tournaments_can_be_synced(conn, client_for, clock):
    manual = add_tournament(conn)

    with pytest.raises(LookupError):
        run(sync_tournament(conn, client_for, manual, clock))
    with pytest.raises(LookupError):
        run(sync_tournament(conn, client_for, 99999, clock))


# --- solo tournaments ---


def test_a_solo_tournament_has_one_team_per_player(conn, client_for, clock):
    tid = do_import(conn, client_for, clock, "synthetic-solo").tournament_id

    teams = conn.execute(
        "SELECT t.name, COUNT(p.osu_id) AS n FROM teams t JOIN team_players p ON p.team_id = t.id "
        "WHERE t.tournament_id = ? GROUP BY t.id ORDER BY t.name",
        (tid,),
    ).fetchall()

    assert [tuple(row) for row in teams] == [("AdminAmy", 1), ("CaptainCarl", 1), ("RivalRex", 1)]
    assert tournament_row(conn, 9002)["is_solo"] == 1
    assert tournament_row(conn, 9002)["gamemode"] == 0


def test_in_a_solo_tournament_our_team_is_the_admins_own_entry_and_only_they_join(
    conn, client_for, clock
):
    tid = do_import(conn, client_for, clock, "synthetic-solo").tournament_id

    ours = conn.execute(
        "SELECT t.name FROM teams t "
        "WHERE t.id = (SELECT our_team_id FROM tournaments WHERE id = ?)",
        (tid,),
    ).fetchone()
    assert ours[0] == "AdminAmy"
    assert members(conn, tid) == {ADMIN_ID: ("captain", "wybin")}


def test_a_second_solo_sync_adds_no_teams(conn, client_for, clock):
    tid = do_import(conn, client_for, clock, "synthetic-solo").tournament_id
    before = counts(conn)

    run(sync_tournament(conn, client_for, tid, clock))

    assert counts(conn) == before


# --- sync_active ---


def test_sync_active_updates_only_active_wybin_tournaments(conn, fake, client_for, clock):
    active = do_import(conn, client_for, clock).tournament_id
    solo = do_import(conn, client_for, clock, "synthetic-solo").tournament_id
    conn.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (solo,))
    manual = add_tournament(conn, "By hand")
    fake.requests.clear()

    synced = run(sync_active(conn, client_for, clock))

    assert synced == 1
    assert "/api/v1/tournament/synthetic-cup" in fake.paths()
    assert "/api/v1/tournament/synthetic-solo" not in fake.paths()
    assert active and manual


def test_one_failing_tournament_does_not_stop_the_others(conn, fake, client_for, clock):
    first = do_import(conn, client_for, clock).tournament_id
    do_import(conn, client_for, clock, "synthetic-solo")
    del fake.tournaments["synthetic-cup"]  # now a 404 for the first
    fake.tournaments["synthetic-solo"]["name"] = "Solo renamed"

    with pytest.raises(WybinSyncError, match="1 of 2"):
        run(sync_active(conn, client_for, clock))

    assert tournament_row(conn, 9002)["name"] == "Solo renamed"
    assert tournament_row(conn)["id"] == first


# --- sign-up tracking ---


def link_admin(conn, osu_id=ADMIN_ID, wybin_user_id=100011):
    links_repo.upsert(conn, osu_id, wybin_user_id)


def test_tracking_imports_tournaments_the_admin_signed_up_for(conn, client_for, clock):
    link_admin(conn)

    result = run(track_signups(conn, client_for, clock))

    assert result.imported == 2
    assert {row[0] for row in conn.execute("SELECT wybin_id FROM tournaments")} == {9001, 9002}
    assert tournament_row(conn)["owner_osu_id"] == ADMIN_ID
    assert (
        conn.execute(
            "SELECT name FROM teams WHERE is_ours = 1 AND tournament_id = ?",
            (tournament_row(conn)["id"],),
        ).fetchone()[0]
        == "Alpha Squad"
    )
    assert links_repo.get(conn, ADMIN_ID).last_checked_at == "2026-11-12T12:30:15Z"


def test_tracking_skips_what_is_already_known(conn, fake, client_for, clock):
    link_admin(conn)
    do_import(conn, client_for, clock)
    fake.requests.clear()

    result = run(track_signups(conn, client_for, clock))

    assert result.imported == 1  # only the solo tournament is new
    assert "/api/v1/tournament/synthetic-cup" not in fake.paths()


def test_tracking_never_brings_back_a_hidden_tournament(conn, fake, client_for, clock):
    link_admin(conn)
    tid = do_import(conn, client_for, clock).tournament_id
    conn.execute("UPDATE tournaments SET status = 'hidden' WHERE id = ?", (tid,))
    fake.requests.clear()

    run(track_signups(conn, client_for, clock))

    assert tournament_row(conn)["status"] == "hidden"
    assert "/api/v1/tournament/synthetic-cup" not in fake.paths()


def test_tracking_does_nothing_without_linked_admins(conn, fake, client_for, clock):
    result = run(track_signups(conn, client_for, clock))

    assert result.imported == 0
    assert fake.requests == []


def test_tournaments_that_ended_long_ago_are_imported_as_archived(conn, fake, client_for, clock):
    link_admin(conn)
    for stage in fake.tournaments["synthetic-cup"]["stages"]:
        stage["endDate"] = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)

    run(track_signups(conn, client_for, clock))

    row = tournament_row(conn)
    assert row["status"] == "archived"
    assert row["archived_at"] == "2026-11-12T12:30:15Z"
    assert tournament_row(conn, 9002)["status"] == "active"
    # They are history, so the poller leaves them alone.
    fake.requests.clear()
    run(sync_active(conn, client_for, clock))
    assert "/api/v1/tournament/synthetic-cup" not in fake.paths()


def test_a_tournament_that_ended_recently_stays_active(conn, fake, client_for, clock):
    link_admin(conn)
    for stage in fake.tournaments["synthetic-cup"]["stages"]:
        stage["endDate"] = int((clock.current - timedelta(days=10)).timestamp() * 1000)

    run(track_signups(conn, client_for, clock))

    assert tournament_row(conn)["status"] == "active"


def test_an_explicit_import_of_an_old_tournament_stays_active(conn, fake, client_for, clock):
    for stage in fake.tournaments["synthetic-cup"]["stages"]:
        stage["endDate"] = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)

    do_import(conn, client_for, clock)

    assert tournament_row(conn)["status"] == "active"


def test_a_failing_sign_up_check_is_reported_but_the_others_still_run(
    conn, fake, client_for, clock
):
    add_user(conn, 4002, "RivalRia")
    links_repo.upsert(conn, 4002, 404404)  # wybin has no such user
    link_admin(conn)

    with pytest.raises(WybinSyncError):
        run(track_signups(conn, client_for, clock))

    assert tournament_row(conn) is not None  # the admin with a working link was still handled
    assert links_repo.get(conn, 4002).last_checked_at is None


def test_one_tournament_failing_to_import_does_not_stop_the_rest(conn, fake, client_for, clock):
    link_admin(conn)
    del fake.tournaments["synthetic-cup"]

    with pytest.raises(WybinSyncError):
        run(track_signups(conn, client_for, clock))

    assert tournament_row(conn, 9002) is not None


# --- build_pool ---


def beatmap(order, beatmap_id=1000, mods=0, placeholder=False):
    return WybinBeatmap(
        order=order,
        beatmap_id=None if placeholder else beatmap_id + order,
        beatmapset_id=None,
        artist=None,
        title=None,
        version=None,
        mapper=None,
        mods_bitmask=mods,
        star_rating=None,
        bpm=None,
        length_s=None,
        cs=None,
        ar=None,
        od=None,
        hp=None,
        is_placeholder=placeholder,
        wybin_entry_id=None,
    )


def test_two_brackets_with_the_same_prefix_do_not_collide():
    pool = build_pool(
        [
            WybinBracket("NoMod", "None", [beatmap(1), beatmap(2)]),
            WybinBracket("NoFail", None, [beatmap(1), beatmap(2)]),
        ],
        0,
    )

    assert list(pool) == ["NM1", "NM2", "NM3", "NM4"]


def test_several_tiebreakers_are_numbered_and_a_single_one_is_plain_tb():
    two = build_pool([WybinBracket("Tiebreaker", "Tiebreaker", [beatmap(1), beatmap(2)])], 0)
    one = build_pool([WybinBracket("Tiebreaker", "Tiebreaker", [beatmap(1)])], 0)

    assert list(two) == ["TB1", "TB2"]
    assert list(one) == ["TB"]


def test_placeholders_are_left_out_and_numbers_follow_wybins_order():
    pool = build_pool(
        [WybinBracket("Hidden", "Hidden", [beatmap(3), beatmap(1, placeholder=True)])], 0
    )

    assert list(pool) == ["HD3"]


def test_required_mods_fall_back_to_the_bracket_when_the_bitmask_is_empty():
    pool = build_pool([WybinBracket("Hidden", "Hidden", [beatmap(1, mods=0)])], 0)

    assert pool["HD1"]["required_mods"] == "HD"


def test_freemod_and_tiebreaker_maps_require_no_mods_even_with_a_bitmask():
    pool = build_pool(
        [
            WybinBracket("Freemod", "Freemod", [beatmap(1, mods=8)]),
            WybinBracket("Tiebreaker", "Tiebreaker", [beatmap(1, mods=64)]),
        ],
        0,
    )

    assert pool["FM1"]["required_mods"] == "" and pool["TB"]["required_mods"] == ""


def test_required_mods_use_the_games_mod_table():
    mania = build_pool([WybinBracket("Hidden", "Hidden", [beatmap(1, mods=8 | 16 | 32768)])], 3)
    standard = build_pool([WybinBracket("Hidden", "Hidden", [beatmap(1, mods=8 | 16)])], 0)

    assert mania["HD1"]["required_mods"] == "HDK4"  # Hard Rock does not exist in mania
    assert standard["HD1"]["required_mods"] == "HDHR"


def test_a_bracket_name_with_an_impossible_mod_requires_nothing():
    pool = build_pool([WybinBracket("Zzzz Zzzz", None, [beatmap(1)])], 0)

    assert next(iter(pool.values()))["required_mods"] == ""


# --- archived tournaments are not rewritten by an import ---


def test_importing_an_archived_tournament_is_refused_and_changes_nothing(
    conn, fake, client_for, clock
):
    tid = do_import(conn, client_for, clock).tournament_id
    archive_service.archive(conn, tid, "Top 8", clock.now())
    fake.tournaments["synthetic-cup"]["name"] = "Rewritten"
    snapshot = counts(conn), tuple(tournament_row(conn))
    fake.requests.clear()

    with pytest.raises(TournamentArchivedError):
        do_import(conn, client_for, clock)

    assert (counts(conn), tuple(tournament_row(conn))) == snapshot
    assert fake.requests == []  # refused before anything was fetched


def test_an_archived_tournament_found_by_its_wybin_id_is_refused_too(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    archive_service.archive(conn, tid, "", clock.now())
    conn.execute("UPDATE tournaments SET slug = 'old-slug' WHERE id = ?", (tid,))
    fake.tournaments["synthetic-cup"]["name"] = "Rewritten"

    with pytest.raises(TournamentArchivedError):
        do_import(conn, client_for, clock)

    assert tournament_row(conn)["name"] == "Synthetic Catch Cup"


def test_a_sync_of_an_archived_tournament_is_refused(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    archive_service.archive(conn, tid, "", clock.now())
    fake.tournaments["synthetic-cup"]["name"] = "Rewritten"

    with pytest.raises(TournamentArchivedError):
        run(sync_tournament(conn, client_for, tid, clock))

    assert tournament_row(conn)["name"] == "Synthetic Catch Cup"


def test_importing_a_hidden_tournament_still_refreshes_it(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    archive_service.hide(conn, tid)
    fake.tournaments["synthetic-cup"]["name"] = "Refreshed"

    do_import(conn, client_for, clock)

    assert tournament_row(conn)["name"] == "Refreshed"
    assert tournament_row(conn)["status"] == "hidden"


# --- deleted tournaments are not tracked back in ---


def test_a_deleted_tournament_is_not_imported_again_by_tracking(conn, fake, client_for, clock):
    link_admin(conn)
    run(track_signups(conn, client_for, clock))
    cup = tournament_row(conn)["id"]
    archive_service.delete_tournament(conn, cup)
    fake.requests.clear()

    result = run(track_signups(conn, client_for, clock))

    assert result.imported == 0
    assert tournament_row(conn) is None
    assert "/api/v1/tournament/synthetic-cup" not in fake.paths()
    assert tournament_row(conn, 9002) is not None  # the other one was not deleted


def test_a_deleted_tournament_known_only_by_slug_is_skipped_too(conn, fake, client_for, clock):
    link_admin(conn)
    run(track_signups(conn, client_for, clock))
    archive_service.delete_tournament(conn, tournament_row(conn)["id"])
    for ref in fake.users[100011]["participantTournaments"]:
        del ref["id"]  # the sign-up list names no ids

    run(track_signups(conn, client_for, clock))

    assert tournament_row(conn) is None


def test_importing_a_deleted_tournament_by_hand_works_and_makes_it_known_again(
    conn, fake, client_for, clock
):
    link_admin(conn)
    run(track_signups(conn, client_for, clock))
    archive_service.delete_tournament(conn, tournament_row(conn)["id"])
    assert deleted_repo.is_deleted(conn, 9001, "synthetic-cup")

    do_import(conn, client_for, clock)

    assert tournament_row(conn) is not None
    assert not deleted_repo.is_deleted(conn, 9001, "synthetic-cup")
    fake.requests.clear()
    run(track_signups(conn, client_for, clock))
    assert "/api/v1/tournament/synthetic-cup" not in fake.paths()


# --- reordered pools keep their rows ---


def test_a_reordered_pool_keeps_each_row_on_its_beatmap(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    before = {
        row["beatmap_id"]: (row["id"], row["slot"])
        for row in slots(conn, "Qualifiers", tid).values()
    }
    nomod = fake.mappools[9001]["stages"][0]["modBrackets"][0]["beatmaps"]
    for entry in nomod:
        entry["beatmapOrder"] = 3 - entry["beatmapOrder"]  # NM1 and NM2 trade places

    run(sync_tournament(conn, client_for, tid, clock))

    after = {
        row["beatmap_id"]: (row["id"], row["slot"])
        for row in slots(conn, "Qualifiers", tid).values()
    }
    assert after[910001] == (before[910001][0], "NM2")
    assert after[910002] == (before[910002][0], "NM1")
    assert {k: v for k, v in after.items() if k not in (910001, 910002)} == {
        k: v for k, v in before.items() if k not in (910001, 910002)
    }


def test_a_rotated_pool_keeps_ids_and_new_maps_get_new_rows(conn, fake, client_for, clock):
    tid = do_import(conn, client_for, clock).tournament_id
    ids = {row["beatmap_id"]: row["id"] for row in slots(conn, "Qualifiers", tid).values()}
    hidden = fake.mappools[9001]["stages"][0]["modBrackets"][1]["beatmaps"]
    hidden.append(
        {**hidden[0], "id": 99, "beatmapOrder": 3, "beatmapId": 919000, "name": "New - Map [Fresh]"}
    )
    del fake.mappools[9001]["stages"][0]["modBrackets"][2]  # HR1 removed

    run(sync_tournament(conn, client_for, tid, clock))

    pool = slots(conn, "Qualifiers", tid)
    assert pool["HD3"]["beatmap_id"] == 919000 and pool["HD3"]["id"] not in ids.values()
    assert "HR1" not in pool and 910005 not in {r["beatmap_id"] for r in pool.values()}
    assert pool["HD1"]["id"] == ids[910003]  # the untouched ones keep their rows
    assert conn.execute("SELECT COUNT(*) FROM pool_maps WHERE slot LIKE '#%'").fetchone()[0] == 0


def test_the_same_beatmap_twice_keeps_one_row_each(conn, fake, client_for, clock):
    brackets = fake.mappools[9001]["stages"][0]["modBrackets"]
    brackets[1]["beatmaps"][0]["beatmapId"] = brackets[1]["beatmaps"][1]["beatmapId"]
    tid = do_import(conn, client_for, clock).tournament_id
    ids = {slot: row["id"] for slot, row in slots(conn, "Qualifiers", tid).items()}

    run(sync_tournament(conn, client_for, tid, clock))

    assert {slot: row["id"] for slot, row in slots(conn, "Qualifiers", tid).items()} == ids


# --- an unreadable mappool endpoint is reported in the job state ---


def test_a_missing_pool_endpoint_marks_the_job_while_the_rest_is_still_synced(
    conn, fake, client_for, clock
):
    tid = do_import(conn, client_for, clock).tournament_id
    fake.mappools.clear()
    fake.tournaments["synthetic-cup"]["name"] = "Renamed"

    with pytest.raises(WybinSyncError, match="map pool could not be updated for 1"):
        run(sync_active(conn, client_for, clock))

    assert tournament_row(conn)["name"] == "Renamed"  # teams, stages and so on were refreshed
    assert len(slots(conn, "Qualifiers", tid)) == 8  # the stored pool stayed


def test_a_readable_pool_endpoint_is_not_reported(conn, client_for, clock):
    do_import(conn, client_for, clock)

    assert run(sync_active(conn, client_for, clock)) == 1
