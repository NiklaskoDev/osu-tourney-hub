"""Who can open and change what. The matrix runs every role against every tournament route."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.models import Member, Role
from app.services.access import effective_role, is_temp_captain_active, role_for, role_label
from tests.support import (
    ADMIN_ID,
    add_match,
    add_member,
    add_pool_map,
    add_stage,
    add_team,
    add_tournament,
    add_user,
)

NOW = datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC)
LATER = "2026-11-12T13:30:00Z"
EARLIER = "2026-11-12T12:00:00Z"


def member(role: str, temp: str | None = None, substitute: bool = False) -> Member:
    return Member(1, 7, "Sample", None, role, substitute, temp, "manual")


# --- pure role rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "temp", "expected"),
    [
        ("captain", None, Role.CAPTAIN),
        ("co_captain", None, Role.CAPTAIN),
        ("player", None, Role.PLAYER),
        ("player", LATER, Role.CAPTAIN),
        ("player", EARLIER, Role.PLAYER),
        ("player", "2026-11-12T12:30:15Z", Role.PLAYER),  # ends exactly now: already over
        ("player", "2026-11-12T12:30:16Z", Role.CAPTAIN),
        ("player", "not a time", Role.PLAYER),  # malformed never grants access
        ("guest", None, Role.GUEST),
        ("guest", LATER, Role.GUEST),  # only players can be temporary captains
    ],
)
def test_effective_role(stored, temp, expected):
    assert effective_role(member(stored, temp), NOW) == expected


def test_substitutes_keep_their_role():
    assert effective_role(member("player", substitute=True), NOW) == Role.PLAYER


def test_temp_captain_flag_only_applies_to_players():
    assert is_temp_captain_active(member("player", LATER), NOW)
    assert not is_temp_captain_active(member("captain", LATER), NOW)
    assert not is_temp_captain_active(member("player", None), NOW)


def test_role_ranking():
    assert Role.GUEST < Role.PLAYER < Role.CAPTAIN < Role.ADMIN


def test_admin_outranks_everyone_and_needs_no_membership():
    assert role_for(True, None, NOW) == Role.ADMIN
    assert role_for(True, member("guest"), NOW) == Role.ADMIN


def test_a_stranger_has_no_role():
    assert role_for(False, None, NOW) is None


def test_role_labels():
    assert role_label(member("co_captain")) == "Co-captain"
    assert role_label(None) == "Admin"


# --- the matrix --------------------------------------------------------------------------------

TARGET = 2099  # a player the managers act on
GUEST_TO_ADD = 3001

ROLES = {
    "admin": (ADMIN_ID, None, None),
    "captain": (2001, "captain", None),
    "co_captain": (2002, "co_captain", None),
    "temp_captain_active": (2003, "player", LATER),
    "temp_captain_expired": (2004, "player", EARLIER),
    "player": (2005, "player", None),
    "substitute": (2006, "player", None),
    "guest": (2007, "guest", None),
    "non_member": (2008, None, None),
    "anonymous": (None, None, None),
}
MANAGERS = {"admin", "captain", "co_captain", "temp_captain_active"}
READERS = MANAGERS | {"temp_captain_expired", "player", "substitute", "guest"}


def expected_view(case: str) -> int:
    if case in READERS:
        return 200
    return 303 if case == "anonymous" else 404


def expected_write(case: str) -> int:
    if case in MANAGERS:
        return 303
    if case == "anonymous":
        return 403  # no session, so no CSRF token
    return 404 if case == "non_member" else 403


@pytest.fixture
def arranged(db, osu):
    tournament_id = add_tournament(db)
    for case, (osu_id, role, temp) in ROLES.items():
        if role:
            add_member(
                db,
                tournament_id,
                osu_id,
                role,
                is_substitute=(case == "substitute"),
                temp_captain_until=temp,
            )
    add_user(db, ADMIN_ID, "AdminAmy")
    add_user(db, 2008, "Stranger")
    add_member(db, tournament_id, TARGET, "player")
    osu.add_user(GUEST_TO_ADD, "NewGuest")
    return tournament_id


@pytest.fixture
def act_as(client, login, csrf):
    def _act_as(case: str) -> dict[str, str]:
        osu_id = ROLES[case][0]
        client.cookies.clear()
        if osu_id is None:
            return {}
        login(osu_id)
        return csrf()

    return _act_as


@pytest.mark.parametrize("case", ROLES)
def test_viewing_pages(case, arranged, client, act_as):
    act_as(case)

    for path in (
        f"/t/{arranged}",
        f"/t/{arranged}/members",
        f"/t/{arranged}/pool",
        f"/t/{arranged}/lineup",
    ):
        assert client.get(path, follow_redirects=False).status_code == expected_view(case), path


@pytest.mark.parametrize("case", ROLES)
def test_changing_the_roster(case, arranged, client, act_as, db):
    token = act_as(case)
    base = f"/t/{arranged}/members"
    allowed = case in MANAGERS

    added = client.post(base, data={**token, "osu_id": str(GUEST_TO_ADD)}, follow_redirects=False)
    updated = client.post(
        f"{base}/{TARGET}", data={**token, "role": "co_captain"}, follow_redirects=False
    )
    removed = client.post(f"{base}/{TARGET}/remove", data=token, follow_redirects=False)

    assert (added.status_code, updated.status_code, removed.status_code) == (
        (expected_write(case),) * 3
    ), case
    in_roster = db.execute(
        "SELECT COUNT(*) FROM tournament_members WHERE tournament_id = ? AND osu_id = ?",
        (arranged, GUEST_TO_ADD),
    ).fetchone()[0]
    assert in_roster == int(allowed)
    target_row = db.execute(
        "SELECT role FROM tournament_members WHERE tournament_id = ? AND osu_id = ?",
        (arranged, TARGET),
    ).fetchone()
    assert (target_row is None) == allowed  # removed last, so only managers get this far


@pytest.fixture
def pool(arranged, db):
    stage = add_stage(db, arranged)
    return stage, add_pool_map(db, stage, "NM1")


def expected_player_write(case: str) -> int:
    """Players and above add scores and notes; guests are read-only."""
    if case in READERS - {"guest"}:
        return 303
    if case == "anonymous":
        return 403
    return 404 if case == "non_member" else 403


@pytest.mark.parametrize("case", ROLES)
def test_adding_scores_and_notes(case, arranged, pool, client, act_as, db):
    token = act_as(case)
    stage, pool_map = pool
    base = f"/t/{arranged}"
    player = {"osu_id": str(TARGET)} if case == "admin" else {}  # an admin has no roster entry

    score = client.post(
        f"{base}/scores",
        data={**token, **player, "pool_map_id": str(pool_map), "score": "800000", "accuracy": "97"},
        follow_redirects=False,
    )
    note = client.post(
        f"{base}/notes",
        data={**token, "pool_map_id": str(pool_map), "body": "hi", "visibility": "team"},
        follow_redirects=False,
    )

    assert (score.status_code, note.status_code) == (expected_player_write(case),) * 2, case
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == int(score.status_code == 303)
    assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == int(note.status_code == 303)


@pytest.mark.parametrize("case", ROLES)
def test_saving_lineups(case, arranged, pool, client, act_as, db):
    token = act_as(case)
    stage, pool_map = pool
    base = f"/t/{arranged}/lineup"

    copied = client.post(
        f"{base}/copy-suggestion", data={**token, "stage": str(stage)}, follow_redirects=False
    )
    saved = client.post(
        base,
        data={**token, "stage": str(stage), "status": "final", f"pick_{pool_map}_0": str(TARGET)},
        follow_redirects=False,
    )

    assert (copied.status_code, saved.status_code) == (expected_write(case),) * 2, case
    expected_rows = int(case in MANAGERS)
    assert db.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == expected_rows


@pytest.fixture
def our_match(arranged, db):
    """A match of our team in a best-of-3 stage with one ban, and its NM1 map."""
    ours = add_team(db, arranged, "Alpha Squad", ours=True)
    rivals = add_team(db, arranged, "Rival Crew")
    stage = add_stage(db, arranged, "Round of 16")
    db.execute("UPDATE stages SET best_of = 3, bans = 1 WHERE id = ?", (stage,))
    pool_map = add_pool_map(db, stage, "NM1")
    match = add_match(db, arranged, ours, rivals, "2026-11-14T18:00:00Z", stage_id=stage)
    return match, pool_map


@pytest.mark.parametrize("case", ROLES)
def test_viewing_matches_and_pickems(case, arranged, our_match, client, act_as, db):
    act_as(case)
    match, _ = our_match

    rivals = db.execute("SELECT id FROM teams WHERE name = 'Rival Crew'").fetchone()[0]
    paths = (
        f"/t/{arranged}/matches/{match}",
        f"/t/{arranged}/pickems",
        f"/t/{arranged}/matches/{match}/review",
        f"/t/{arranged}/opponents/{rivals}",
    )
    for path in paths:
        assert client.get(path, follow_redirects=False).status_code == expected_view(case), path


@pytest.mark.parametrize("case", ROLES)
def test_saving_a_match_plan(case, arranged, our_match, client, act_as, db):
    token = act_as(case)
    match, pool_map = our_match

    saved = client.post(
        f"/t/{arranged}/matches/{match}/plan",
        data={**token, "ban_1": str(pool_map)},
        follow_redirects=False,
    )

    assert saved.status_code == expected_write(case), case
    rows = db.execute("SELECT COUNT(*) FROM plan_items").fetchone()[0]
    assert rows == int(case in MANAGERS)


@pytest.mark.parametrize("case", ROLES)
def test_correcting_and_reading_match_events(case, arranged, our_match, client, act_as, db):
    token = act_as(case)
    match, pool_map = our_match
    base = f"/t/{arranged}/matches/{match}"

    corrected = client.post(
        f"{base}/events",
        data={**token, "new_kind": "ban", "new_side": "1", "new_map": str(pool_map)},
        follow_redirects=False,
    )
    refreshed = client.post(f"{base}/review/refresh", data=token, follow_redirects=False)

    assert corrected.status_code == expected_write(case), case
    # the match has no lobby, so a manager is refused with 409 and everyone else as for any write
    assert refreshed.status_code == (409 if case in MANAGERS else expected_write(case)), case
    assert db.execute("SELECT COUNT(*) FROM match_events").fetchone()[0] == int(case in MANAGERS)


def test_a_non_member_cannot_tell_a_real_tournament_from_a_missing_one(client, arranged, act_as):
    act_as("non_member")

    real = client.get(f"/t/{arranged}")
    missing = client.get("/t/9999")

    assert real.status_code == missing.status_code == 404
    assert real.text == missing.text


def test_unknown_and_malformed_tournament_ids_are_not_found(client, login):
    login(ADMIN_ID)

    for path in ("/t/9999", "/t/abc", "/t/-1", "/t/9999/members"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("text/html"), path


def test_a_temporary_captain_loses_the_powers_when_the_time_runs_out(
    arranged, client, act_as, clock
):
    token = act_as("temp_captain_active")
    members = f"/t/{arranged}/members/{TARGET}"
    assert client.post(members, data={**token, "role": "player"}).status_code == 200

    clock.current += timedelta(hours=1, minutes=1)  # past LATER

    denied = client.post(members, data={**token, "role": "player"}, follow_redirects=False)
    assert denied.status_code == 403
    assert client.get(f"/t/{arranged}/members").status_code == 200  # still a player: can read


def test_admin_status_follows_the_environment_on_every_request(arranged, client, login, settings):
    login(1002)
    assert client.get("/styleguide").status_code == 200

    object.__setattr__(settings, "admin_osu_ids", frozenset({ADMIN_ID}))

    assert client.get("/styleguide").status_code == 403


def test_a_hidden_tournament_is_only_visible_to_admins(db, client, login):
    tournament_id = add_tournament(db, status="hidden")
    add_member(db, tournament_id, 2001, "captain")

    login(2001)
    assert client.get(f"/t/{tournament_id}").status_code == 404
    assert client.get(f"/t/{tournament_id}/members").status_code == 404
    assert "Spring Cup" not in client.get("/").text

    login(ADMIN_ID)
    assert client.get(f"/t/{tournament_id}").status_code == 200


def test_an_archived_tournament_can_be_read_but_not_changed(db, client, login, csrf):
    tournament_id = add_tournament(db, status="archived")
    add_member(db, tournament_id, 2001, "captain")
    add_member(db, tournament_id, 2002, "player")
    base = f"/t/{tournament_id}/members"

    for actor in (2001, ADMIN_ID):
        login(actor)
        token = csrf()
        assert client.get(base).status_code == 200
        for path, data in (
            (base, {"osu_id": "5"}),
            (f"{base}/2002", {"role": "guest"}),
            (f"{base}/2002/remove", {}),
        ):
            response = client.post(path, data={**token, **data}, follow_redirects=False)
            assert response.status_code == 409, (actor, path)
            assert "archived" in response.text

    role = db.execute("SELECT role FROM tournament_members WHERE osu_id = 2002").fetchone()[0]
    assert role == "player"


def test_an_archived_tournament_answers_a_guest_with_403_before_409(db, client, login, csrf):
    tournament_id = add_tournament(db, status="archived")
    add_member(db, tournament_id, 2007, "guest")
    login(2007)

    response = client.post(f"/t/{tournament_id}/members/2007", data={**csrf(), "role": "captain"})

    assert response.status_code == 403


def test_the_dashboard_lists_only_the_tournaments_a_user_belongs_to(db, client, login):
    mine = add_tournament(db, "Mine Cup")
    add_tournament(db, "Not Mine Cup")
    add_member(db, mine, 2001, "player")

    login(2001)
    page = client.get("/").text

    assert "Mine Cup" in page
    assert "Not Mine Cup" not in page


def test_admins_see_every_tournament_with_their_own_role_where_they_have_one(db, client, login):
    add_tournament(db, "First Cup")
    second = add_tournament(db, "Second Cup", status="archived")
    add_tournament(db, "Secret Cup", status="hidden")
    add_member(db, second, ADMIN_ID, "player")

    login(ADMIN_ID)
    page = client.get("/").text

    for name in ("First Cup", "Second Cup", "Secret Cup"):
        assert name in page
    assert page.index("First Cup") < page.index("Second Cup")  # archived ones come last


def test_the_dashboard_explains_an_empty_list(client, login):
    login(2001)

    assert "not on a tournament roster yet" in client.get("/").text


def test_roles_are_labelled_on_the_dashboard_and_overview(db, client, login):
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2002, "co_captain")

    login(2002)

    assert "Co-captain" in client.get("/").text
    assert "Co-captain" in client.get(f"/t/{tournament_id}").text
