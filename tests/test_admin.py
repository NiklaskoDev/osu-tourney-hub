import re
from datetime import timedelta

import pytest

from app.repos import deleted_tournaments as deleted_repo
from app.repos import jobs as jobs_repo
from app.repos import wybin_links as links_repo
from app.workers.jobs import SYNC_ACTIVE, TRACK_SIGNUPS
from app.workers.scheduler import GENERIC_ERROR_TEXT
from tests.support import ADMIN_ID, add_member, add_tournament


@pytest.fixture(autouse=True)
def fake(wybin):
    wybin.load_defaults()
    return wybin


@pytest.fixture
def as_admin(client, login, csrf, db):
    login(ADMIN_ID, "AdminAmy")
    return csrf()


def post(client, path, data=None, token=None, **kwargs):
    return client.post(
        path, data={**(token or {}), **(data or {})}, follow_redirects=False, **kwargs
    )


def import_cup(client, token, link="synthetic-cup"):
    return post(client, "/admin/tournaments/import", {"link": link}, token)


def tournament_id(db, wybin_id=9001):
    return db.execute("SELECT id FROM tournaments WHERE wybin_id = ?", (wybin_id,)).fetchone()[0]


# --- access ---


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin"),
        ("post", "/admin/wybin-link"),
        ("post", "/admin/wybin-link/remove"),
        ("post", "/admin/tournaments/import"),
        ("post", "/admin/tournaments"),
        ("post", "/admin/tournaments/1/archive"),
        ("post", "/admin/tournaments/1/unarchive"),
        ("post", "/admin/tournaments/1/hide"),
        ("post", "/admin/tournaments/1/sync"),
        ("post", "/admin/tournaments/1/delete"),
        ("get", "/admin/tournaments/1"),
        ("post", "/admin/tournaments/1/stages"),
        ("post", "/admin/tournaments/1/stages/1/remove"),
        ("post", "/admin/tournaments/1/maps"),
        ("post", "/admin/tournaments/1/maps/1/remove"),
    ],
)
def test_only_admins_reach_the_admin_routes(client, login, csrf, db, method, path):
    add_tournament(db)
    login(2001, "CaptainCarl")
    data = csrf() if method == "post" else None

    response = getattr(client, method)(
        path, **({"data": data} if data else {}), follow_redirects=False
    )

    assert response.status_code == 403


def test_anonymous_visitors_are_sent_to_the_login(client):
    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


def test_admin_posts_need_the_csrf_token(client, as_admin, db):
    response = client.post("/admin/tournaments/import", data={"link": "synthetic-cup"})

    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 0


def test_the_top_bar_shows_the_admin_link_only_to_admins(client, login):
    login(ADMIN_ID)
    assert 'href="/admin"' in client.get("/").text
    login(2001)
    assert 'href="/admin"' not in client.get("/").text


# --- the console ---


def test_the_console_lists_every_tournament_with_its_state(client, as_admin, db):
    add_tournament(db, "Spring Cup", acronym="SC")
    add_tournament(db, "Old Cup", status="archived", acronym="OC")
    add_tournament(db, "Secret Cup", status="hidden", acronym="SE")

    page = client.get("/admin").text

    for name in ("Spring Cup", "Old Cup", "Secret Cup"):
        assert name in page
    assert "Archived" in page and "Hidden" in page and "Active" in page


def test_the_console_shows_job_state_with_generic_errors(client, as_admin, db):
    page = client.get("/admin").text
    assert page.count("Waiting for the first run") == 2

    jobs_repo.record_start(db, SYNC_ACTIVE, "2026-11-12T12:00:00Z")
    jobs_repo.record_failure(db, SYNC_ACTIVE, GENERIC_ERROR_TEXT)
    jobs_repo.record_start(db, TRACK_SIGNUPS, "2026-11-12T12:00:00Z")
    jobs_repo.record_success(db, TRACK_SIGNUPS, "2026-11-12T12:00:01Z")
    page = client.get("/admin").text

    assert "Failed." in page and GENERIC_ERROR_TEXT in page
    assert "Update active wybin tournaments" in page
    assert re.search(r"result--win\">OK<", page)


# --- import ---


def test_importing_from_wybin_opens_the_new_tournament(client, as_admin, db):
    response = import_cup(client, as_admin)

    tid = tournament_id(db)
    assert response.status_code == 303 and response.headers["location"] == f"/t/{tid}"
    page = client.get(f"/t/{tid}").text
    assert "Synthetic Catch Cup imported." in page
    assert "Alpha Squad" in page and "osu!catch" in page
    assert "Qualifiers" in page and "Round of 16" in page and "Finals" in page


def test_importing_by_address_works(client, as_admin, db):
    import_cup(client, as_admin, "https://wybin.xyz/synthetic-cup/home")

    assert tournament_id(db)


def test_importing_the_same_tournament_again_says_so(client, as_admin, db):
    import_cup(client, as_admin)
    import_cup(client, as_admin)
    page = client.get(f"/t/{tournament_id(db)}").text

    assert "was already imported and is up to date" in page
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 1


def test_a_link_that_is_not_wybin_shows_an_inline_error_and_keeps_the_input(client, as_admin, db):
    response = import_cup(client, as_admin, "https://evil.example/x")

    assert response.status_code == 422
    assert "Only https://wybin.xyz addresses are accepted." in response.text
    assert 'value="https://evil.example/x"' in response.text
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 0


def test_an_empty_import_asks_for_an_address(client, as_admin):
    response = import_cup(client, as_admin, "   ")

    assert response.status_code == 422
    assert "Enter a wybin address or tournament slug." in response.text


def test_an_unknown_tournament_is_explained(client, as_admin):
    response = import_cup(client, as_admin, "nothing-here")

    assert response.status_code == 422
    assert "wybin has no tournament with this address." in response.text


def test_when_wybin_is_down_nothing_is_imported_and_the_page_says_so(client, as_admin, db, fake):
    fake.down = True

    response = import_cup(client, as_admin)

    assert response.status_code == 502
    assert "wybin did not respond. Nothing was changed; try again later." in response.text
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 0


def test_an_unreadable_wybin_answer_is_reported_without_details(client, as_admin, db, fake):
    fake.tournaments["synthetic-cup"]["stages"] = "broken"

    response = import_cup(client, as_admin)

    assert response.status_code == 502
    assert "wybin sent data this app cannot read." in response.text
    assert "broken" not in response.text
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 0


def test_the_importing_admin_lands_on_our_team_and_the_roster_gets_access(
    client, as_admin, db, login
):
    import_cup(client, as_admin)
    tid = tournament_id(db)

    login(3002, "PlayerPia")  # on the roster, never logged in before
    assert client.get(f"/t/{tid}").status_code == 200
    login(4001, "RivalRex")  # on an opponent team: not a member
    assert client.get(f"/t/{tid}").status_code == 404


# --- wybin account link ---


def test_linking_a_wybin_account_imports_the_signed_up_tournaments(client, as_admin, db):
    response = post(client, "/admin/wybin-link", {"wybin_user_id": "100011"}, as_admin)

    assert response.status_code == 303
    assert links_repo.get(db, ADMIN_ID).wybin_user_id == 100011
    assert {row[0] for row in db.execute("SELECT wybin_id FROM tournaments")} == {9001, 9002}
    assert jobs_repo.get(db, TRACK_SIGNUPS).last_ok_at is not None
    page = client.get("/admin").text
    assert "Linked to wybin user" in page and "100011" in page


@pytest.mark.parametrize("entered", ["", "abc", "0", "-5", "12.5", str(2**31), "１２３"])
def test_a_wybin_user_id_must_be_a_positive_number(client, as_admin, db, entered):
    response = post(client, "/admin/wybin-link", {"wybin_user_id": entered}, as_admin)

    assert response.status_code == 422
    assert "Enter your wybin user id as a number." in response.text
    assert links_repo.get(db, ADMIN_ID) is None


def test_an_unknown_wybin_user_is_not_linked(client, as_admin, db):
    response = post(client, "/admin/wybin-link", {"wybin_user_id": "999"}, as_admin)

    assert response.status_code == 422
    assert "wybin has no user with this id." in response.text
    assert 'value="999"' in response.text
    assert links_repo.get(db, ADMIN_ID) is None


def test_linking_fails_clearly_when_wybin_is_down(client, as_admin, db, fake):
    fake.down = True

    response = post(client, "/admin/wybin-link", {"wybin_user_id": "100011"}, as_admin)

    assert response.status_code == 502
    assert links_repo.get(db, ADMIN_ID) is None


def test_two_admins_cannot_share_one_wybin_account(client, as_admin, db, login, csrf):
    post(client, "/admin/wybin-link", {"wybin_user_id": "100011"}, as_admin)
    login(1002, "SecondAdmin")

    response = post(client, "/admin/wybin-link", {"wybin_user_id": "100011"}, csrf())

    assert response.status_code == 409
    assert "Another admin already uses this wybin account." in response.text


def test_unlinking_keeps_the_imported_tournaments(client, as_admin, db):
    post(client, "/admin/wybin-link", {"wybin_user_id": "100011"}, as_admin)

    response = post(client, "/admin/wybin-link/remove", token=as_admin)

    assert response.status_code == 303
    assert links_repo.get(db, ADMIN_ID) is None
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 2


# --- manual tournaments ---


def create(client, token, **overrides):
    data = {"name": "Winter Open", "acronym": "WO", "gamemode": "3", **overrides}
    return post(client, "/admin/tournaments", data, token)


def test_a_manual_tournament_gets_a_team_and_the_creator_as_captain(client, as_admin, db):
    response = create(client, as_admin, team_size_min="2", team_size_max="4")

    row = db.execute("SELECT * FROM tournaments WHERE name = 'Winter Open'").fetchone()
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/tournaments/{row['id']}"
    assert (row["acronym"], row["gamemode"], row["source"], row["status"]) == (
        "WO",
        3,
        "manual",
        "active",
    )
    assert (row["is_solo"], row["team_size_min"], row["team_size_max"]) == (0, 2, 4)
    assert row["owner_osu_id"] == ADMIN_ID and row["wybin_id"] is None
    team = db.execute(
        "SELECT name, is_ours FROM teams WHERE tournament_id = ?", (row["id"],)
    ).fetchone()
    assert tuple(team) == ("Our team", 1)
    assert row["our_team_id"] is not None
    member = db.execute(
        "SELECT role, source FROM tournament_members WHERE tournament_id = ? AND osu_id = ?",
        (row["id"], ADMIN_ID),
    ).fetchone()
    assert tuple(member) == ("captain", "manual")
    assert "Tournament created" in client.get(response.headers["location"]).text


def test_a_solo_tournament_has_team_size_one_and_a_team_named_after_the_creator(
    client, as_admin, db
):
    create(client, as_admin, is_solo="1", team_size_min="3", team_size_max="5", name="Solo Cup")

    row = db.execute("SELECT * FROM tournaments WHERE name = 'Solo Cup'").fetchone()
    assert (row["is_solo"], row["team_size_min"], row["team_size_max"]) == (1, 1, 1)
    assert (
        db.execute("SELECT name FROM teams WHERE tournament_id = ?", (row["id"],)).fetchone()[0]
        == "AdminAmy"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": "  "}, "Enter a name for the tournament."),
        ({"name": "x" * 101}, "Keep this under 100 characters."),
        ({"acronym": "x" * 17}, "Keep the acronym under 16 characters."),
        ({"gamemode": "4"}, "The game mode must be a whole number from 0 to 3."),
        ({"gamemode": ""}, "Choose a game mode."),
        ({"gamemode": "abc"}, "The game mode must be a whole number from 0 to 3."),
        ({"team_size_min": "5", "team_size_max": "3"}, "The smallest team cannot be bigger"),
        ({"team_size_min": "0"}, "The smallest team must be a whole number from 1 to 16."),
        ({"team_size_max": "17"}, "The largest team must be a whole number from 1 to 16."),
    ],
)
def test_an_invalid_tournament_form_shows_the_message_and_keeps_the_input(
    client, as_admin, db, overrides, message
):
    response = create(client, as_admin, **overrides)

    assert response.status_code == 422
    assert message in response.text
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 0
    typed = overrides.get("name", "Winter Open")
    if typed.strip() and len(typed) <= 100:
        assert f'value="{typed}"' in response.text


# --- archive, restore, hide ---


@pytest.fixture
def spring(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, 2001, "captain")
    return tid


def status_of(db, tid):
    return tuple(
        db.execute(
            "SELECT status, archived_at, final_placing FROM tournaments WHERE id = ?", (tid,)
        ).fetchone()
    )


def test_archiving_stores_the_time_and_the_final_placing(client, as_admin, db, spring):
    response = post(
        client, f"/admin/tournaments/{spring}/archive", {"final_placing": " Top 8 "}, as_admin
    )

    assert response.status_code == 303
    assert status_of(db, spring) == ("archived", "2026-11-12T12:30:15Z", "Top 8")
    assert "Spring Cup is archived" in client.get("/admin").text


def test_archiving_without_a_placing_leaves_it_empty(client, as_admin, db, spring):
    post(client, f"/admin/tournaments/{spring}/archive", {"final_placing": ""}, as_admin)

    assert status_of(db, spring)[2] is None


def test_a_too_long_placing_is_refused(client, as_admin, db, spring):
    response = post(
        client, f"/admin/tournaments/{spring}/archive", {"final_placing": "x" * 61}, as_admin
    )

    assert response.status_code == 422
    assert "Keep the final placing under 60 characters." in response.text
    assert status_of(db, spring)[0] == "active"


def test_archiving_twice_is_a_conflict(client, as_admin, db, spring):
    post(client, f"/admin/tournaments/{spring}/archive", token=as_admin)

    response = post(client, f"/admin/tournaments/{spring}/archive", token=as_admin)

    assert response.status_code == 409
    assert "already archived" in response.text


def test_restoring_makes_it_active_again_and_keeps_the_placing(client, as_admin, db, spring):
    post(client, f"/admin/tournaments/{spring}/archive", {"final_placing": "Champions"}, as_admin)

    response = post(client, f"/admin/tournaments/{spring}/unarchive", token=as_admin)

    assert response.status_code == 303
    assert status_of(db, spring) == ("active", None, "Champions")
    assert post(client, f"/admin/tournaments/{spring}/unarchive", token=as_admin).status_code == 409


def test_hiding_removes_it_from_members_but_not_from_admins_and_it_can_be_restored(
    client, as_admin, db, spring, login, csrf
):
    post(client, f"/admin/tournaments/{spring}/hide", token=as_admin)
    assert status_of(db, spring)[0] == "hidden"
    assert post(client, f"/admin/tournaments/{spring}/hide", token=as_admin).status_code == 409
    assert client.get(f"/t/{spring}").status_code == 200  # still visible to the admin
    login(2001, "CaptainCarl")
    assert client.get(f"/t/{spring}").status_code == 404
    assert "Spring Cup" not in client.get("/").text

    login(ADMIN_ID)
    post(client, f"/admin/tournaments/{spring}/unarchive", token=csrf())

    assert status_of(db, spring)[0] == "active"


@pytest.mark.parametrize("action", ["archive", "unarchive", "hide", "sync", "delete"])
def test_actions_on_an_unknown_tournament_are_not_found(client, as_admin, action):
    response = post(client, f"/admin/tournaments/9999/{action}", token=as_admin)

    assert response.status_code == 404


# --- sync now ---


def test_sync_now_refreshes_from_wybin(client, as_admin, db, fake):
    import_cup(client, as_admin)
    fake.tournaments["synthetic-cup"]["name"] = "Renamed Cup"
    tid = tournament_id(db)

    response = post(client, f"/admin/tournaments/{tid}/sync", token=as_admin)

    assert response.status_code == 303
    assert (
        db.execute("SELECT name FROM tournaments WHERE id = ?", (tid,)).fetchone()[0]
        == "Renamed Cup"
    )
    assert "Renamed Cup is up to date with wybin." in client.get("/admin").text


def test_sync_now_rejects_manual_and_archived_tournaments(client, as_admin, db, spring):
    manual = post(client, f"/admin/tournaments/{spring}/sync", token=as_admin)
    import_cup(client, as_admin)
    tid = tournament_id(db)
    post(client, f"/admin/tournaments/{tid}/archive", token=as_admin)
    archived = post(client, f"/admin/tournaments/{tid}/sync", token=as_admin)

    assert manual.status_code == 409 and "did not come from wybin" in manual.text
    assert archived.status_code == 409 and "Restore it before syncing." in archived.text


def test_sync_now_with_wybin_down_changes_nothing(client, as_admin, db, fake):
    import_cup(client, as_admin)
    tid = tournament_id(db)
    fake.tournaments["synthetic-cup"]["name"] = "Renamed Cup"
    fake.down = True

    response = post(client, f"/admin/tournaments/{tid}/sync", token=as_admin)

    assert response.status_code == 502
    assert "wybin did not respond." in response.text
    assert (
        db.execute("SELECT name FROM tournaments WHERE id = ?", (tid,)).fetchone()[0]
        == "Synthetic Catch Cup"
    )


# --- delete ---


def test_the_delete_dialog_says_what_is_deleted_and_what_is_kept(client, as_admin, spring):
    page = client.get("/admin").text

    assert "lineups and notes" in page
    assert "Scores are kept, but they lose their link to this tournament" in page


def test_deleting_needs_the_acronym_typed_and_removes_everything(client, as_admin, db):
    import_cup(client, as_admin)
    tid = tournament_id(db)

    wrong = post(client, f"/admin/tournaments/{tid}/delete", {"confirm": "scc "}, as_admin)
    assert wrong.status_code == 422
    assert "type SCC exactly to confirm" in wrong.text
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 1

    right = post(client, f"/admin/tournaments/{tid}/delete", {"confirm": " SCC "}, as_admin)

    assert right.status_code == 303
    for table in (
        "tournaments",
        "stages",
        "pool_maps",
        "teams",
        "team_players",
        "tournament_members",
    ):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    assert "were deleted" in client.get("/admin").text


def test_a_tournament_without_an_acronym_is_confirmed_by_its_name(client, as_admin, db):
    tid = add_tournament(db, "Nameless Cup", acronym="")
    db.execute("UPDATE tournaments SET acronym = NULL WHERE id = ?", (tid,))

    wrong = post(client, f"/admin/tournaments/{tid}/delete", {"confirm": "Nope"}, as_admin)
    right = post(client, f"/admin/tournaments/{tid}/delete", {"confirm": "Nameless Cup"}, as_admin)

    assert wrong.status_code == 422 and "type Nameless Cup exactly" in wrong.text
    assert right.status_code == 303


def test_deleting_needs_a_login_from_the_last_five_minutes(client, as_admin, db, spring, clock):
    clock.current += timedelta(minutes=6)

    response = post(client, f"/admin/tournaments/{spring}/delete", {"confirm": "SC"}, as_admin)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/admin"
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 1


def test_deleting_works_right_up_to_the_five_minute_limit(client, as_admin, db, spring, clock):
    clock.current += timedelta(minutes=5)

    response = post(client, f"/admin/tournaments/{spring}/delete", {"confirm": "SC"}, as_admin)

    assert response.headers["location"] == "/admin"
    assert db.execute("SELECT COUNT(*) FROM tournaments").fetchone()[0] == 0


# --- archived tournaments are not rewritten, hidden ones not exposed by archiving ---


def test_importing_an_archived_tournament_again_is_a_conflict_and_changes_nothing(
    client, as_admin, db, fake
):
    import_cup(client, as_admin)
    tid = tournament_id(db)
    post(client, f"/admin/tournaments/{tid}/archive", {"final_placing": "Top 8"}, as_admin)
    fake.tournaments["synthetic-cup"]["name"] = "Rewritten"

    response = import_cup(client, as_admin)

    assert response.status_code == 409
    assert "This tournament is archived. Restore it before syncing." in response.text
    assert 'value="synthetic-cup"' in response.text
    row = db.execute("SELECT name, status, final_placing FROM tournaments").fetchone()
    assert tuple(row) == ("Synthetic Catch Cup", "archived", "Top 8")


def test_importing_by_address_hits_the_same_refusal(client, as_admin, db):
    import_cup(client, as_admin)
    post(client, f"/admin/tournaments/{tournament_id(db)}/archive", token=as_admin)

    response = import_cup(client, as_admin, "https://wybin.xyz/synthetic-cup/home")

    assert response.status_code == 409


def test_a_restored_tournament_can_be_imported_again(client, as_admin, db):
    import_cup(client, as_admin)
    tid = tournament_id(db)
    post(client, f"/admin/tournaments/{tid}/archive", token=as_admin)
    post(client, f"/admin/tournaments/{tid}/unarchive", token=as_admin)

    assert import_cup(client, as_admin).status_code == 303


def test_an_archived_tournament_cannot_be_hidden_which_would_make_it_visible_to_nobody_else(
    client, as_admin, db, spring
):
    post(client, f"/admin/tournaments/{spring}/archive", token=as_admin)

    response = post(client, f"/admin/tournaments/{spring}/hide", token=as_admin)

    assert response.status_code == 409
    assert "Restore it before hiding it." in response.text
    assert status_of(db, spring)[0] == "archived"


def test_a_hidden_tournament_cannot_be_archived_until_it_is_restored(client, as_admin, db, spring):
    post(client, f"/admin/tournaments/{spring}/hide", token=as_admin)

    response = post(client, f"/admin/tournaments/{spring}/archive", token=as_admin)

    assert response.status_code == 409
    assert "Restore it before archiving it." in response.text
    assert status_of(db, spring)[0] == "hidden"
    post(client, f"/admin/tournaments/{spring}/unarchive", token=as_admin)
    assert post(client, f"/admin/tournaments/{spring}/archive", token=as_admin).status_code == 303


# --- unreadable map pool ---

POOL_WARNING = "Teams and stages were updated; the map pool could not be read."


def test_importing_with_an_unreadable_pool_warns_instead_of_reporting_plain_success(
    client, as_admin, db, fake
):
    fake.mappools.clear()

    response = import_cup(client, as_admin)

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert POOL_WARNING in page and "alert--warning" in page
    assert "Synthetic Catch Cup imported." not in page
    assert db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 3


def test_sync_now_with_an_unreadable_pool_warns_instead_of_reporting_plain_success(
    client, as_admin, db, fake
):
    import_cup(client, as_admin)
    tid = tournament_id(db)
    fake.mappools.clear()

    response = post(client, f"/admin/tournaments/{tid}/sync", token=as_admin)

    assert response.status_code == 303
    page = client.get("/admin").text
    assert POOL_WARNING in page and "alert--warning" in page
    assert "is up to date with wybin." not in page


def test_a_readable_pool_still_reports_plain_success(client, as_admin, db):
    import_cup(client, as_admin)

    page = client.get(f"/t/{tournament_id(db)}").text

    assert "Synthetic Catch Cup imported." in page and POOL_WARNING not in page


# --- deleting a wybin tournament ---


def test_deleting_a_wybin_tournament_is_remembered_and_the_dialog_says_what_happens_next(
    client, as_admin, db
):
    import_cup(client, as_admin)
    tid = tournament_id(db)
    assert "not imported again automatically" in client.get("/admin").text

    post(client, f"/admin/tournaments/{tid}/delete", {"confirm": "SCC"}, as_admin)

    assert deleted_repo.is_deleted(db, 9001, "synthetic-cup")
    assert import_cup(client, as_admin).status_code == 303  # by hand it still works
    assert not deleted_repo.is_deleted(db, 9001, "synthetic-cup")


def test_a_failed_delete_remembers_nothing(client, as_admin, db):
    import_cup(client, as_admin)

    post(client, f"/admin/tournaments/{tournament_id(db)}/delete", {"confirm": "nope"}, as_admin)

    assert not deleted_repo.is_deleted(db, 9001, "synthetic-cup")


def test_deleting_a_tournament_made_by_hand_remembers_nothing(client, as_admin, db, spring):
    post(client, f"/admin/tournaments/{spring}/delete", {"confirm": "SC"}, as_admin)

    assert (
        db.execute(
            "SELECT COUNT(*) FROM app_settings WHERE key = ?", (deleted_repo.KEY,)
        ).fetchone()[0]
        == 0
    )


# --- very long digit input is a message, never a crash ---

HUGE = "9" * 5000


@pytest.mark.parametrize(
    ("path", "data", "message"),
    [
        ("/admin/wybin-link", {"wybin_user_id": HUGE}, "Enter your wybin user id as a number."),
        ("/admin/tournaments", {"name": "A", "gamemode": HUGE}, "The game mode must be"),
        (
            "/admin/tournaments",
            {"name": "A", "gamemode": "0", "team_size_min": HUGE},
            "The smallest team must be a whole number",
        ),
        ("/admin/tournaments/{spring}/stages", {"name": "S", "best_of": HUGE}, "Best of must be"),
        ("/admin/tournaments/{spring}/stages", {"name": "S", "bans": HUGE}, "Bans must be"),
        ("/admin/tournaments/{spring}/maps", {"stage_id": HUGE, "slot": "NM1"}, "Choose the stage"),
        (
            "/admin/tournaments/{spring}/maps",
            {"stage_id": "1", "slot": "NM1", "beatmap": HUGE},
            "Enter a beatmap link or id",
        ),
        (
            "/admin/tournaments/{spring}/maps",
            {"stage_id": "1", "slot": "NM1", "beatmap": f"https://osu.ppy.sh/b/{HUGE}"},
            "Enter a beatmap link or id",
        ),
        ("/t/{spring}/members", {"osu_id": HUGE}, "Enter the osu! user id as a number"),
        (
            "/t/{spring}/settings",
            {"freemod_rule": "none", "scoring": "v2", "our_team": HUGE},
            "Choose one of the teams",
        ),
        (
            "/t/{spring}/settings",
            {"freemod_rule": "none", "scoring": "v2", "lineup_size_1": HUGE},
            "Lineup size must be",
        ),
    ],
)
def test_a_very_long_digit_string_gets_an_inline_error_not_a_server_error(
    client, as_admin, db, spring, path, data, message
):
    db.execute(
        "INSERT INTO stages (id, tournament_id, name, sort_order, is_qualifier) "
        "VALUES (1, ?, 'S1', 1, 0)",
        (spring,),
    )

    response = post(client, path.format(spring=spring), data, as_admin)

    assert response.status_code == 422
    assert message in response.text


def test_a_digit_string_in_a_lineup_field_name_is_ignored(client, as_admin, db, spring):
    response = post(
        client,
        f"/t/{spring}/settings",
        {"freemod_rule": "none", "scoring": "v2", f"lineup_size_{HUGE}": "3"},
        as_admin,
    )

    assert response.status_code == 303
