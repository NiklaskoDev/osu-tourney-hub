import pytest

from tests.support import ADMIN_ID, add_member, add_tournament


@pytest.fixture
def tid(db) -> int:
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2001, "captain")
    add_member(db, tournament_id, 2002, "player")
    return tournament_id


@pytest.fixture
def as_captain(client, login, csrf, tid):
    login(2001, "CaptainCarl")
    return csrf()


def stored(db, tid, osu_id):
    row = db.execute(
        "SELECT role, is_substitute, temp_captain_until, source, added_by "
        "FROM tournament_members WHERE tournament_id = ? AND osu_id = ?",
        (tid, osu_id),
    ).fetchone()
    return tuple(row) if row else None


# --- viewing ---


def test_everyone_on_the_roster_sees_the_list_without_edit_controls(db, client, login, tid):
    add_member(db, tid, 2003, "guest")
    for viewer in (2002, 2003):
        login(viewer)

        page = client.get(f"/t/{tid}/members").text

        assert "player2002" in page
        assert "CaptainCarl" in page or "player2001" in page
        assert "Save" not in page
        assert "Remove" not in page
        assert "Add a guest" not in page


def test_captains_get_the_edit_controls(client, as_captain, tid):
    page = client.get(f"/t/{tid}/members").text

    assert "Add a guest" in page
    assert page.count("Save") >= 2
    assert f'action="/t/{tid}/members/2002/remove"' in page


def test_the_list_is_ordered_by_role_then_name(db, client, login, tid):
    add_member(db, tid, 2003, "guest")
    add_member(db, tid, 2004, "co_captain")
    login(2001)

    page = client.get(f"/t/{tid}/members").text

    order = [page.index(f"player{osu_id}") for osu_id in (2001, 2004, 2002, 2003)]
    assert order == sorted(order)


def test_temporary_captains_are_marked_with_their_end_time(db, client, as_captain, tid):
    add_member(db, tid, 2003, "player", temp_captain_until="2026-11-12T13:30:00Z")
    add_member(db, tid, 2004, "player", temp_captain_until="2026-11-12T12:00:00Z")

    page = client.get(f"/t/{tid}/members").text

    assert "Acts as captain until 2026-11-12 13:30 UTC" in page
    assert "Captain period ended 2026-11-12 12:00 UTC" in page


def test_a_roster_entry_without_a_known_name_shows_the_id(db, client, as_captain, tid):
    db.execute(
        "INSERT INTO tournament_members (tournament_id, osu_id, role) VALUES (?, 777, 'guest')",
        (tid,),
    )

    assert "osu! user 777" in client.get(f"/t/{tid}/members").text


# --- role, substitute and temporary captain ---


def test_a_captain_can_change_a_role(client, as_captain, tid, db):
    response = client.post(f"/t/{tid}/members/2002", data={**as_captain, "role": "co_captain"})

    assert response.status_code == 200
    assert "Saved changes for player2002" in response.text
    assert stored(db, tid, 2002)[0] == "co_captain"


def test_an_invalid_role_is_refused_and_nothing_changes(client, as_captain, tid, db):
    response = client.post(f"/t/{tid}/members/2002", data={**as_captain, "role": "owner"})

    assert response.status_code == 422
    assert "Choose one of the listed roles" in response.text
    assert stored(db, tid, 2002)[0] == "player"


def test_the_substitute_flag_persists_and_can_be_cleared(client, as_captain, tid, db):
    client.post(
        f"/t/{tid}/members/2002", data={**as_captain, "role": "player", "is_substitute": "1"}
    )
    assert stored(db, tid, 2002)[1] == 1
    assert "checked" in client.get(f"/t/{tid}/members").text

    client.post(f"/t/{tid}/members/2002", data={**as_captain, "role": "player"})

    assert stored(db, tid, 2002)[1] == 0


def test_a_guest_cannot_be_a_substitute(client, as_captain, tid, db):
    client.post(
        f"/t/{tid}/members/2002", data={**as_captain, "role": "guest", "is_substitute": "1"}
    )

    assert stored(db, tid, 2002)[:2] == ("guest", 0)


def test_a_temporary_captain_period_is_stored_in_utc_and_can_be_cleared(
    client, as_captain, tid, db
):
    data = {**as_captain, "role": "player"}

    client.post(f"/t/{tid}/members/2002", data={**data, "temp_captain_until": "2026-11-13T20:00"})
    assert stored(db, tid, 2002)[2] == "2026-11-13T20:00:00Z"
    assert 'value="2026-11-13T20:00"' in client.get(f"/t/{tid}/members").text

    client.post(f"/t/{tid}/members/2002", data={**data, "temp_captain_until": ""})
    assert stored(db, tid, 2002)[2] is None


def test_a_temporary_captain_can_manage_the_roster_until_the_time_runs_out(
    client, as_captain, tid, db, login, csrf, clock
):
    client.post(
        f"/t/{tid}/members/2002",
        data={**as_captain, "role": "player", "temp_captain_until": "2026-11-12T13:00"},
    )
    login(2002)
    token = csrf()

    assert (
        client.post(f"/t/{tid}/members/2001", data={**token, "role": "captain"}).status_code == 200
    )


@pytest.mark.parametrize("text", ["tomorrow", "2026-13-40T99:99", "2026-11-13", "2026-11-13 20:00"])
def test_a_malformed_captain_period_is_refused(client, as_captain, tid, db, text):
    response = client.post(
        f"/t/{tid}/members/2002",
        data={**as_captain, "role": "player", "temp_captain_until": text},
    )

    assert response.status_code == 422
    assert "date and time" in response.text
    assert stored(db, tid, 2002)[2] is None


def test_the_captain_period_is_dropped_when_the_role_is_not_player(client, as_captain, tid, db):
    client.post(
        f"/t/{tid}/members/2002",
        data={**as_captain, "role": "co_captain", "temp_captain_until": "2026-11-13T20:00"},
    )

    assert stored(db, tid, 2002)[:3] == ("co_captain", 0, None)


def test_changing_someone_who_is_not_on_the_roster_is_a_404(client, as_captain, tid):
    response = client.post(f"/t/{tid}/members/4242", data={**as_captain, "role": "guest"})

    assert response.status_code == 404


def test_a_member_of_another_tournament_cannot_be_changed_here(client, as_captain, tid, db):
    other = add_tournament(db, "Other Cup")
    add_member(db, other, 2005, "player")

    response = client.post(f"/t/{tid}/members/2005", data={**as_captain, "role": "captain"})

    assert response.status_code == 404
    assert stored(db, other, 2005)[0] == "player"


# --- protections ---


def test_the_last_captain_cannot_be_demoted(client, login, csrf, tid, db):
    login(ADMIN_ID)  # an admin may change anyone, but the rule still holds
    token = csrf()

    response = client.post(f"/t/{tid}/members/2001", data={**token, "role": "player"})

    assert response.status_code == 409
    assert "at least one captain" in response.text
    assert stored(db, tid, 2001)[0] == "captain"


def test_a_captain_can_be_demoted_when_another_captain_remains(client, as_captain, tid, db):
    add_member(db, tid, 2003, "captain")

    response = client.post(f"/t/{tid}/members/2003", data={**as_captain, "role": "player"})

    assert response.status_code == 200
    assert stored(db, tid, 2003)[0] == "player"


def test_the_last_captain_cannot_be_removed(client, login, csrf, tid, db):
    login(ADMIN_ID)

    response = client.post(f"/t/{tid}/members/2001/remove", data=csrf())

    assert response.status_code == 409
    assert stored(db, tid, 2001) is not None


def test_a_co_captain_does_not_count_as_the_captain(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "co_captain")
    login(2003)

    response = client.post(f"/t/{tid}/members/2001", data={**csrf(), "role": "co_captain"})

    assert response.status_code == 409
    assert stored(db, tid, 2001)[0] == "captain"


def test_nobody_but_an_admin_changes_their_own_role(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "captain")  # so the last-captain rule is not what refuses this
    login(2001)

    response = client.post(f"/t/{tid}/members/2001", data={**csrf(), "role": "player"})

    assert response.status_code == 403
    assert "own role" in response.text
    assert stored(db, tid, 2001)[0] == "captain"


def test_a_temporary_captain_cannot_extend_their_own_term(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "player", temp_captain_until="2026-11-12T13:00:00Z")
    login(2003)

    response = client.post(
        f"/t/{tid}/members/2003",
        data={**csrf(), "role": "player", "temp_captain_until": "2099-01-01T00:00"},
    )

    assert response.status_code == 403
    assert stored(db, tid, 2003)[2] == "2026-11-12T13:00:00Z"


def test_a_co_captain_cannot_promote_themselves(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "co_captain")
    login(2003)

    response = client.post(f"/t/{tid}/members/2003", data={**csrf(), "role": "captain"})

    assert response.status_code == 403
    assert stored(db, tid, 2003)[0] == "co_captain"


def test_changing_only_your_own_substitute_flag_is_allowed(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "co_captain")
    login(2003)

    response = client.post(
        f"/t/{tid}/members/2003", data={**csrf(), "role": "co_captain", "is_substitute": "1"}
    )

    assert response.status_code == 200
    assert stored(db, tid, 2003)[:2] == ("co_captain", 1)


def test_an_unchanged_term_can_be_saved_by_its_holder(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "player", temp_captain_until="2026-11-12T13:00:00Z")
    login(2003)

    response = client.post(
        f"/t/{tid}/members/2003",
        data={
            **csrf(),
            "role": "player",
            "is_substitute": "1",
            "temp_captain_until": "2026-11-12T13:00",
        },
    )

    assert response.status_code == 200
    assert stored(db, tid, 2003)[1:3] == (1, "2026-11-12T13:00:00Z")


def test_an_admin_may_change_their_own_membership(client, db, login, csrf, tid):
    add_member(db, tid, ADMIN_ID, "player")
    login(ADMIN_ID)

    response = client.post(f"/t/{tid}/members/{ADMIN_ID}", data={**csrf(), "role": "co_captain"})

    assert response.status_code == 200
    assert stored(db, tid, ADMIN_ID)[0] == "co_captain"


def test_players_cannot_change_roles_and_guests_cannot_post_anything(client, db, login, csrf, tid):
    add_member(db, tid, 2003, "guest")
    for actor in (2002, 2003):
        login(actor)
        token = csrf()
        for path, data in (
            (f"/t/{tid}/members/2002", {"role": "captain"}),
            (f"/t/{tid}/members/2001/remove", {}),
            (f"/t/{tid}/members", {"osu_id": "3001"}),
        ):
            response = client.post(path, data={**token, **data}, follow_redirects=False)
            assert response.status_code == 403, (actor, path)

    assert stored(db, tid, 2002)[0] == "player"
    assert stored(db, tid, 2001)[0] == "captain"
    assert stored(db, tid, 3001) is None


# --- removing ---


def test_a_captain_can_remove_a_member(client, as_captain, tid, db):
    response = client.post(f"/t/{tid}/members/2002/remove", data=as_captain)

    assert response.status_code == 200
    assert "player2002 was removed" in response.text
    assert stored(db, tid, 2002) is None


def test_removing_someone_who_is_not_on_the_roster_is_a_404(client, as_captain, tid):
    assert client.post(f"/t/{tid}/members/4242/remove", data=as_captain).status_code == 404


def test_the_removal_dialog_asks_for_confirmation(client, as_captain, tid):
    page = client.get(f"/t/{tid}/members").text

    assert "Remove player2002?" in page
    assert "<dialog" in page


# --- adding guests ---


def test_a_guest_is_added_after_checking_the_id_with_osu(client, as_captain, tid, db, osu):
    osu.add_user(3001, "NewGuest")

    response = client.post(f"/t/{tid}/members", data={**as_captain, "osu_id": " 3001 "})

    assert response.status_code == 200
    assert "NewGuest was added as a guest" in response.text
    assert stored(db, tid, 3001) == ("guest", 0, None, "manual", 2001)
    name = db.execute("SELECT username, last_login_at FROM users WHERE osu_id = 3001").fetchone()
    assert tuple(name) == ("NewGuest", None)  # known by name, but has never logged in
    assert osu.calls("GET", "/users/3001")


def test_a_person_who_is_already_on_the_roster_is_not_added_twice(client, as_captain, tid, osu):
    osu.add_user(2002, "player2002")

    response = client.post(f"/t/{tid}/members", data={**as_captain, "osu_id": "2002"})

    assert response.status_code == 409
    assert "already a member" in response.text


def test_an_unknown_osu_id_is_reported_next_to_the_field_and_kept(client, as_captain, tid, db):
    response = client.post(f"/t/{tid}/members", data={**as_captain, "osu_id": "9999"})

    assert response.status_code == 422
    assert "No osu! user has this id." in response.text
    assert 'value="9999"' in response.text
    assert stored(db, tid, 9999) is None


@pytest.mark.parametrize("text", ["", "abc", "0", "-3", "12.5", "99999999999", "١٢٣", "3001 3002"])
def test_a_malformed_id_is_refused_without_asking_osu(client, as_captain, tid, osu, text):
    response = client.post(f"/t/{tid}/members", data={**as_captain, "osu_id": text})

    assert response.status_code == 422
    assert "as a number" in response.text
    assert osu.requests == []


def test_when_osu_is_down_nothing_is_added_and_the_message_is_generic(
    client, as_captain, tid, db, osu, osu_delays
):
    osu.add_user(3001, "NewGuest")
    osu.unavailable_paths.add("/oauth/token")

    response = client.post(f"/t/{tid}/members", data={**as_captain, "osu_id": "3001"})

    assert response.status_code == 502
    assert "osu! did not answer" in response.text
    assert "503" not in response.text
    assert stored(db, tid, 3001) is None


def test_the_add_form_is_disabled_for_an_archived_tournament(db, client, login):
    tournament_id = add_tournament(db, status="archived")
    add_member(db, tournament_id, 2001, "captain")
    login(2001)

    page = client.get(f"/t/{tournament_id}/members").text

    assert "The roster is read-only" in page
    assert page.count("disabled") >= 4
