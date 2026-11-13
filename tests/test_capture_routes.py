"""The buttons that ask for score pulls, the capture jobs, and how captured scores show up."""

import asyncio
from datetime import timedelta

import pytest

from app.repos import jobs as jobs_repo
from app.services.wybin_sync import WybinSyncError
from app.workers.jobs import CAPTURE_DRAIN, CAPTURE_ENQUEUE, SYNC_ACTIVE, build_jobs
from app.workers.scheduler import OSU_ERROR_TEXT, Scheduler
from tests.support import (
    ADMIN_ID,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_tournament,
    add_user,
)

CAPTAIN, PLAYER, OTHER_PLAYER, GUEST, STRANGER = 2001, 2002, 2003, 2004, 2009
RIVAL_ONE, RIVAL_TWO = 3001, 3002
LATE_MEMBER = 7777


@pytest.fixture
def cup(db):
    """Spring Cup (osu!, lazer scoring so captured scores count): a captain, two players and a
    guest; one stage with beatmaps 1001 and 1002; and a rival team of two players."""
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    db.execute("UPDATE tournaments SET scoring = 'lazer' WHERE id = ?", (tid,))
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER_PLAYER, "player")
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid)
    add_pool_map(db, stage, "NM1")
    add_pool_map(db, stage, "HD1", required_mods="HD")
    return {"id": tid, "stage": stage, "rivals": add_team(db, tid, "Rivals", RIVAL_ONE, RIVAL_TWO)}


def add_team(db, tid, name, *osu_ids) -> int:
    team = db.execute("INSERT INTO teams (tournament_id, name) VALUES (?, ?)", (tid, name))
    for osu_id in osu_ids:
        db.execute(
            "INSERT INTO team_players (team_id, osu_id, username) VALUES (?, ?, ?)",
            (team.lastrowid, osu_id, f"rival{osu_id}"),
        )
    return int(team.lastrowid)


def captured(db) -> list[tuple[int, int]]:
    rows = db.execute("SELECT osu_id, beatmap_id FROM scores WHERE source = 'api' ORDER BY id")
    return [tuple(row) for row in rows]


def refresh(client, token, tid, **extra):
    return client.post(f"/t/{tid}/capture/refresh", data={**token, **extra}, follow_redirects=False)


def capture_team(client, token, tid, team_id):
    return client.post(f"/t/{tid}/opponents/{team_id}/capture", data=token, follow_redirects=False)


# --- Refresh my scores ---


def test_a_player_refreshing_pulls_their_own_scores_right_away(client, login, csrf, db, osu, cup):
    osu.add_api_score(1001, PLAYER, 9001)
    osu.add_api_score(1001, OTHER_PLAYER, 9002)  # a team mate: not asked for
    login(PLAYER)

    response = refresh(client, csrf(), cup["id"], stage=str(cup["stage"]))

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{cup['id']}/pool?stage={cup['stage']}"
    assert captured(db) == [(PLAYER, 1001)]
    assert "Pulling scores from osu!" in client.get(response.headers["location"]).text
    assert jobs_repo.get(db, CAPTURE_DRAIN).last_ok_at is not None


def test_a_refresh_pulls_again_even_when_a_pull_was_recent(client, login, csrf, db, osu, cup):
    osu.add_api_score(1001, PLAYER, 9001)
    login(PLAYER)
    token = csrf()
    refresh(client, token, cup["id"])
    osu.add_api_score(1001, PLAYER, 9002, total_score=900_000)
    client.app.state.refresh_limiter._hits.clear()  # the 10 minute limit has its own test

    refresh(client, token, cup["id"])

    assert [row[0] for row in db.execute("SELECT source_ref FROM scores ORDER BY id")] == [
        "9001",
        "9002",
    ]


def test_refreshing_twice_within_ten_minutes_is_refused_then_works_again(
    client, login, csrf, clock, cup
):
    login(PLAYER)
    token = csrf()
    assert refresh(client, token, cup["id"]).status_code == 303

    second = refresh(client, token, cup["id"])

    assert second.status_code == 429
    assert "about 10 minutes" in second.text
    assert second.headers["Retry-After"] == "601"
    clock.current += timedelta(minutes=10, seconds=1)
    assert refresh(client, token, cup["id"]).status_code == 303


def test_the_ten_minute_limit_is_per_person(client, login, csrf, cup):
    login(PLAYER)
    refresh(client, csrf(), cup["id"])
    login(OTHER_PLAYER)

    assert refresh(client, csrf(), cup["id"]).status_code == 303


def test_a_pull_that_osu_cannot_answer_is_recorded_generically_and_the_page_still_redirects(
    client, login, csrf, db, osu, cup
):
    osu.unavailable_paths.add("/api/v2/beatmaps/1001/scores/users/2002/all")
    login(PLAYER)

    response = refresh(client, csrf(), cup["id"])

    assert response.status_code == 303
    state = jobs_repo.get(db, CAPTURE_DRAIN)
    assert state.last_error == OSU_ERROR_TEXT and state.last_ok_at is None
    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 2  # tried again later


def test_a_tournament_without_maps_says_so_and_pulls_nothing(client, login, csrf, db, osu):
    tid = add_tournament(db, "Empty Cup", acronym="EC")
    add_member(db, tid, PLAYER, "player")
    login(PLAYER)

    response = refresh(client, csrf(), tid)

    assert response.status_code == 303
    assert "no maps" in client.get(response.headers["location"]).text
    assert osu.requests == []


@pytest.mark.parametrize(
    ("who", "status"),
    [(GUEST, 403), (STRANGER, 404)],
)
def test_refreshing_is_for_players_only(client, login, csrf, db, osu, cup, who, status):
    login(who)

    assert refresh(client, csrf(), cup["id"]).status_code == status
    assert (
        osu.api_scores == {} and db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 0
    )


def test_an_administrator_who_is_not_on_the_roster_has_nothing_to_refresh(
    client, login, csrf, db, cup
):
    login(ADMIN_ID)

    response = refresh(client, csrf(), cup["id"])

    assert response.status_code == 409
    assert "roster" in response.text
    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 0


def test_refreshing_in_an_archived_tournament_is_refused(client, login, csrf, db, cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(PLAYER)

    assert refresh(client, csrf(), cup["id"]).status_code == 409


def test_refreshing_needs_the_csrf_token(client, login, cup):
    login(PLAYER)

    assert refresh(client, {}, cup["id"]).status_code == 403


# --- pulling a whole team ---


def test_a_captain_pulls_the_scores_of_a_whole_team(client, login, csrf, db, osu, cup):
    osu.add_api_score(1001, RIVAL_ONE, 8001)
    osu.add_api_score(1002, RIVAL_TWO, 8002)
    osu.add_api_score(1001, PLAYER, 8003)  # not on that team
    login(CAPTAIN)

    response = capture_team(client, csrf(), cup["id"], cup["rivals"])

    assert response.status_code == 303 and response.headers["location"] == f"/t/{cup['id']}"
    assert captured(db) == [(RIVAL_ONE, 1001), (RIVAL_TWO, 1002)]
    assert "Rivals" in client.get(f"/t/{cup['id']}").text


def test_a_team_can_be_pulled_once_an_hour(client, login, csrf, db, clock, cup):
    other = add_team(db, cup["id"], "Others", 3003)
    login(CAPTAIN)
    token = csrf()
    assert capture_team(client, token, cup["id"], cup["rivals"]).status_code == 303

    again = capture_team(client, token, cup["id"], cup["rivals"])

    assert again.status_code == 429 and "about 60 minutes" in again.text
    assert capture_team(client, token, cup["id"], other).status_code == 303  # another team is fine
    clock.current += timedelta(hours=1, seconds=1)
    assert capture_team(client, token, cup["id"], cup["rivals"]).status_code == 303


def test_only_captains_pull_teams(client, login, csrf, db, osu, cup):
    login(PLAYER)

    assert capture_team(client, csrf(), cup["id"], cup["rivals"]).status_code == 403
    login(STRANGER)
    assert capture_team(client, csrf(), cup["id"], cup["rivals"]).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 0


def test_a_team_of_another_tournament_cannot_be_pulled_here(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    foreign = add_team(db, other, "Foreign", 4001)
    login(CAPTAIN)

    assert capture_team(client, csrf(), cup["id"], foreign).status_code == 404
    assert capture_team(client, csrf(), cup["id"], 99999).status_code == 404


def test_pulling_a_team_in_an_archived_tournament_is_refused(client, login, csrf, db, cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    assert capture_team(client, csrf(), cup["id"], cup["rivals"]).status_code == 409


# --- the jobs ---


def run(coroutine):
    return asyncio.run(coroutine)


def jobs_by_name(app, settings, clock):
    return {j.name: j for j in build_jobs(settings, clock, app.state.wybin, app.state.osu)}


def test_the_hourly_job_queues_and_the_minute_job_pulls(client, db, osu, app, settings, clock, cup):
    osu.add_api_score(1001, PLAYER, 9001)
    jobs = jobs_by_name(app, settings, clock)
    scheduler = Scheduler(settings.db_path, clock, list(jobs.values()))

    assert run(scheduler.run_once(jobs[CAPTURE_ENQUEUE])) is True
    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 6  # 3 players x 2 maps
    assert run(scheduler.run_once(jobs[CAPTURE_DRAIN])) is True

    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 0
    assert captured(db) == [(PLAYER, 1001)]


def test_one_drain_run_pulls_at_most_one_batch(client, db, app, settings, clock, cup):
    for number in range(40):
        db.execute(
            "INSERT INTO capture_queue (osu_id, beatmap_id, gamemode, priority, requested_at) "
            "VALUES (?, 1001, 0, 1, '2026-11-12T12:00:00Z')",
            (5000 + number,),
        )
    jobs = jobs_by_name(app, settings, clock)
    scheduler = Scheduler(settings.db_path, clock, list(jobs.values()))

    run(scheduler.run_once(jobs[CAPTURE_DRAIN]))

    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 10


def test_a_wybin_sync_queues_the_pulls_for_maps_it_brought_in(
    client, db, wybin, app, settings, clock
):
    from app.services.wybin_sync import import_tournament

    wybin.load_defaults()
    jobs = jobs_by_name(app, settings, clock)
    scheduler = Scheduler(settings.db_path, clock, list(jobs.values()))
    add_user(db, ADMIN_ID)
    run(import_tournament(db, app.state.wybin, "synthetic-cup", ADMIN_ID, clock))
    tid = db.execute("SELECT id FROM tournaments WHERE slug = 'synthetic-cup'").fetchone()[0]
    add_member(db, tid, LATE_MEMBER, "player")
    db.execute("DELETE FROM capture_queue")

    run(scheduler.run_once(jobs[SYNC_ACTIVE]))

    pool = {row[0] for row in db.execute("SELECT beatmap_id FROM pool_maps WHERE beatmap_id > 0")}
    pulls = {(row[0], row[1]) for row in db.execute("SELECT osu_id, beatmap_id FROM capture_queue")}
    assert pool and {(LATE_MEMBER, beatmap) for beatmap in pool} <= pulls


def test_a_failed_wybin_sync_records_its_own_error_and_queues_nothing(
    client, db, app, settings, clock, cup, monkeypatch
):
    async def failing_sync(conn, wybin, clock):
        raise WybinSyncError("wybin could not be read for 1 of 1 tournaments.")

    monkeypatch.setattr("app.workers.jobs.sync_active", failing_sync)
    jobs = jobs_by_name(app, settings, clock)
    scheduler = Scheduler(settings.db_path, clock, list(jobs.values()))
    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 0

    assert run(scheduler.run_once(jobs[SYNC_ACTIVE])) is False

    assert (
        jobs_repo.get(db, SYNC_ACTIVE).last_error
        == "wybin could not be read for 1 of 1 tournaments."
    )
    assert db.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0] == 0


# --- on the pool page ---


def test_the_pool_page_offers_refresh_export_and_import_by_role(client, login, cup):
    pool = f"/t/{cup['id']}/pool"
    for who, refresh_button, import_link in (
        (CAPTAIN, True, True),
        (PLAYER, True, False),
        (GUEST, False, False),
    ):
        login(who)

        page = client.get(pool).text

        assert ("Refresh my scores" in page) is refresh_button
        assert ("/import?stage=" in page) is import_link
        assert f"/t/{cup['id']}/export/pool.csv?stage={cup['stage']}" in page  # for everyone


def test_the_pool_page_hides_import_in_an_archived_tournament_and_disables_refresh(
    client, login, db, cup
):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    page = client.get(f"/t/{cup['id']}/pool").text

    assert "/import?stage=" not in page
    assert '<button class="btn btn--secondary" type="submit" disabled>' in page


def test_captured_scores_count_in_lazer_tournaments_and_old_ones_are_marked(client, login, db, cup):
    add_score(
        db, 1001, PLAYER, 800_000, scoring="lazer", source="api", achieved_at="2026-10-01T12:00:00Z"
    )
    add_score(
        db,
        1002,
        PLAYER,
        600_000,
        mods="HD",
        scoring="lazer",
        source="api",
        achieved_at="2026-01-01T12:00:00Z",
    )
    login(PLAYER)

    page = client.get(f"/t/{cup['id']}/pool").text
    sheet, details = page.split("Scores and notes per map")

    assert "800,000" in sheet and "600,000" in sheet  # both count; age only marks
    assert sheet.count('class="old-flag"') == 1  # the January one, as text and not by colour
    assert "osu! API" in details


def test_a_pulled_score_counts_on_a_v2_tournament_marked_lazer_and_feeds_the_team_average(
    client, login, csrf, db, osu, cup
):
    db.execute("UPDATE tournaments SET scoring = 'v2' WHERE id = ?", (cup["id"],))
    osu.add_api_score(1001, PLAYER, 9001, total_score=850_000)
    login(PLAYER)

    refresh(client, csrf(), cup["id"])
    sheet = client.get(f"/t/{cup['id']}/pool").text.split("Scores and notes per map")[0]

    assert captured(db) == [(PLAYER, 1001)]
    assert sheet.count("850,000") >= 3  # the player's cell, the map's team score, the averages
    assert ">lazer</span>" in sheet
    assert "1 player" in sheet  # counted in the team average of the map
