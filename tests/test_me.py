import json
from datetime import timedelta

import pytest

from app.domain.timezones import is_valid_timezone, timezone_names
from tests.support import ADMIN_ID, add_member, add_tournament, add_user, osu_login


def row(db, sql, *args):
    found = db.execute(sql, args).fetchone()
    return tuple(found) if found else None


def test_the_profile_page_needs_a_login(client):
    response = client.get("/me", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=%2Fme"


def test_the_profile_page_shows_the_osu_account(client, login):
    login(2001, "SampleUser")

    page = client.get("/me").text

    assert "SampleUser" in page
    assert "2001" in page
    assert "osu! tokens" in page  # says what is not stored


# --- timezone ---


def test_the_timezone_is_saved_and_shown_as_selected(client, login, csrf, db):
    login(2001)

    response = client.post("/me/timezone", data={**csrf(), "timezone": "Europe/Berlin"})

    assert response.status_code == 200
    assert "Timezone saved: Europe/Berlin." in response.text
    assert '<option value="Europe/Berlin" selected>' in response.text
    assert row(db, "SELECT timezone FROM users WHERE osu_id = 2001") == ("Europe/Berlin",)


def test_a_saved_timezone_can_be_replaced(client, login, csrf, db):
    login(2001)
    client.post("/me/timezone", data={**csrf(), "timezone": "Europe/Berlin"})

    client.post("/me/timezone", data={**csrf(), "timezone": "Asia/Tokyo"})

    assert row(db, "SELECT timezone FROM users WHERE osu_id = 2001") == ("Asia/Tokyo",)


@pytest.mark.parametrize(
    "value", ["", "   ", "Mars/Olympus", "US/Eastern", "Etc/GMT+5", "EST", "utc"]
)
def test_an_invalid_timezone_is_refused_and_the_old_value_kept(client, login, csrf, db, value):
    login(2001)
    client.post("/me/timezone", data={**csrf(), "timezone": "Europe/Berlin"})

    response = client.post("/me/timezone", data={**csrf(), "timezone": value})

    assert response.status_code == 422
    assert "Choose a timezone from the list." in response.text
    assert row(db, "SELECT timezone FROM users WHERE osu_id = 2001") == ("Europe/Berlin",)


def test_the_timezone_field_is_filled_from_the_browser_when_none_is_saved(client, login):
    login(2001)

    page = client.get("/me").text

    assert "data-detect-timezone" in page
    assert '<option value="" selected>Choose your timezone</option>' in page


def test_timezone_names_offer_regions_and_utc_but_no_legacy_aliases():
    names = timezone_names()

    assert names[0] == "UTC"
    assert {"Europe/Berlin", "America/New_York", "Asia/Tokyo", "Australia/Sydney"} <= set(names)
    assert not {"US/Eastern", "Etc/GMT+5", "EST", "GMT", "Zulu"} & set(names)
    assert names[1:] == sorted(names[1:])
    assert is_valid_timezone("Europe/Berlin")
    assert not is_valid_timezone("Europe/Atlantis")


# --- log out everywhere ---


def test_logging_out_everywhere_invalidates_every_older_session(client, osu, csrf, db):
    osu.add_user(2001, "SampleUser")
    osu_login(client, 2001)
    other_device = dict(client.cookies)
    response = client.post("/me/logout-all", data=csrf(), follow_redirects=False)
    assert response.status_code == 303

    assert row(db, "SELECT session_version FROM users WHERE osu_id = 2001") == (2,)
    assert client.get("/me", follow_redirects=False).status_code == 303
    client.cookies.clear()
    for name, value in other_device.items():
        client.cookies.set(name, value, domain="tourney.example.com")
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_after_logging_out_everywhere_a_new_login_works(client, osu, csrf):
    osu.add_user(2001, "SampleUser")
    osu_login(client, 2001)
    client.post("/me/logout-all", data=csrf())

    osu_login(client, 2001)

    assert client.get("/me").status_code == 200


def test_logging_out_everywhere_leaves_other_people_logged_in(client, login, csrf, db):
    login(2001)
    login(2002)
    keep = dict(client.cookies)
    login(2001)
    client.post("/me/logout-all", data=csrf())

    client.cookies.clear()
    for name, value in keep.items():
        client.cookies.set(name, value, domain="tourney.example.com")

    assert client.get("/me").status_code == 200


# --- export ---


def test_the_export_contains_only_the_users_own_data(client, login, db):
    first = add_tournament(db, "First Cup")
    second = add_tournament(db, "Second Cup")
    add_member(db, first, 2001, "captain")
    add_member(db, first, 2002, "player")
    add_member(
        db, second, 2001, "player", is_substitute=True, temp_captain_until="2026-11-13T20:00:00Z"
    )
    add_member(db, second, 2003, "guest")
    db.execute(
        "INSERT INTO feed_tokens (token_hash, osu_id, created_at) VALUES "
        "('hash-of-2001', 2001, '2026-11-01T00:00:00Z'), "
        "('hash-of-2002', 2002, '2026-11-01T00:00:00Z')"
    )
    login(2001, "SampleUser")

    response = client.get("/me/export")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["profile"]["osu_id"] == 2001
    assert data["profile"]["username"] == "SampleUser"
    assert data["tournament_memberships"] == [
        {
            "tournament_id": first,
            "tournament": "First Cup",
            "role": "captain",
            "is_substitute": False,
            "temp_captain_until": None,
            "source": "manual",
        },
        {
            "tournament_id": second,
            "tournament": "Second Cup",
            "role": "player",
            "is_substitute": True,
            "temp_captain_until": "2026-11-13T20:00:00Z",
            "source": "manual",
        },
    ]
    assert data["calendar_feed_tokens"] == [{"created_at": "2026-11-01T00:00:00Z"}]
    text = json.dumps(data)
    for other in ("player2002", "player2003", "hash-of-2001", "hash-of-2002", "session_version"):
        assert other not in text


def test_the_export_needs_a_login(client):
    assert client.get("/me/export", follow_redirects=False).status_code == 303


# --- deletion ---


def seed_user_with_data(db):
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2001, "player")
    add_member(db, tournament_id, 2002, "captain")
    db.execute(
        "UPDATE tournament_members SET added_by = 2001 WHERE osu_id = 2002 AND tournament_id = ?",
        (tournament_id,),
    )
    db.execute("UPDATE tournaments SET owner_osu_id = 2001 WHERE id = ?", (tournament_id,))
    db.execute(
        "INSERT INTO feed_tokens (token_hash, osu_id, created_at) "
        "VALUES ('h', 2001, '2026-11-01T00:00:00Z')"
    )
    return tournament_id


def test_deletion_removes_the_users_rows_and_ends_the_session(client, osu, csrf, db):
    osu.add_user(2001, "SampleUser")
    seed_user_with_data(db)
    osu_login(client, 2001)

    response = client.post("/me/delete", data=csrf(), follow_redirects=False)

    assert response.status_code == 303
    assert row(db, "SELECT COUNT(*) FROM users WHERE osu_id = 2001") == (0,)
    assert row(db, "SELECT COUNT(*) FROM tournament_members WHERE osu_id = 2001") == (0,)
    assert row(db, "SELECT COUNT(*) FROM feed_tokens WHERE osu_id = 2001") == (0,)
    assert client.get("/me", follow_redirects=False).status_code == 303
    assert "Your data was deleted." in client.get("/").text


def test_deletion_leaves_other_peoples_data_and_the_tournament(client, osu, csrf, db):
    osu.add_user(2001, "SampleUser")
    tournament_id = seed_user_with_data(db)
    osu_login(client, 2001)

    client.post("/me/delete", data=csrf())

    assert row(db, "SELECT COUNT(*) FROM users WHERE osu_id = 2002") == (1,)
    assert row(
        db,
        "SELECT role, added_by FROM tournament_members WHERE osu_id = 2002 AND tournament_id = ?",
        tournament_id,
    ) == ("captain", None)
    assert row(db, "SELECT owner_osu_id FROM tournaments WHERE id = ?", tournament_id) == (None,)


def test_a_deleted_user_can_log_in_again_as_a_new_account(client, osu, csrf, db):
    osu.add_user(2001, "SampleUser")
    osu_login(client, 2001)
    client.post("/me/delete", data=csrf())

    osu_login(client, 2001)

    assert client.get("/me").status_code == 200
    assert row(db, "SELECT COUNT(*) FROM tournament_members WHERE osu_id = 2001") == (0,)


def test_cookies_from_before_a_deletion_stay_dead_after_logging_in_again(client, osu, csrf, db):
    osu.add_user(2001, "SampleUser")
    osu_login(client, 2001)
    first = dict(client.cookies)
    osu_login(client, 2001)  # a second device
    second = dict(client.cookies)
    client.post("/me/delete", data=csrf())

    osu_login(client, 2001)
    new_version = row(db, "SELECT session_version FROM users WHERE osu_id = 2001")[0]

    assert new_version > 1
    assert client.get("/me").status_code == 200
    for old in (first, second):
        client.cookies.clear()
        for name, value in old.items():
            client.cookies.set(name, value, domain="tourney.example.com")
        assert client.get("/me", follow_redirects=False).status_code == 303


def test_the_version_floor_survives_repeated_deletions_and_other_users(client, osu, csrf, db):
    for osu_id in (2001, 2002):
        osu.add_user(osu_id, f"user{osu_id}")
    versions = []
    for _ in range(3):
        osu_login(client, 2001)
        versions.append(row(db, "SELECT session_version FROM users WHERE osu_id = 2001")[0])
        client.post("/me/logout-all", data=csrf())  # bump, then delete after logging in again
        osu_login(client, 2001)
        client.post("/me/delete", data=csrf())
    osu_login(client, 2002)

    assert versions == sorted(set(versions))
    assert row(db, "SELECT session_version FROM users WHERE osu_id = 2002")[0] > versions[-1]


def test_deletion_needs_a_login_from_the_last_five_minutes(client, osu, csrf, db, clock):
    osu.add_user(2001, "SampleUser")
    add_user(db, 2002)
    osu_login(client, 2001)
    token = csrf()
    clock.current += timedelta(minutes=5, seconds=1)

    response = client.post("/me/delete", data=token, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/me"
    assert row(db, "SELECT COUNT(*) FROM users WHERE osu_id = 2001") == (1,)
    assert client.get("/me").status_code == 200  # still logged in, just not re-authenticated


def test_deletion_is_allowed_up_to_exactly_five_minutes_after_login(client, osu, csrf, db, clock):
    osu.add_user(2001, "SampleUser")
    osu_login(client, 2001)
    token = csrf()
    clock.current += timedelta(minutes=5)

    response = client.post("/me/delete", data=token, follow_redirects=False)

    assert response.headers["location"] == "/"
    assert row(db, "SELECT COUNT(*) FROM users WHERE osu_id = 2001") == (0,)


def test_logging_in_again_makes_deletion_possible_and_returns_to_the_profile(
    client, osu, csrf, db, clock
):
    osu.add_user(2001, "SampleUser")
    osu_login(client, 2001)
    clock.current += timedelta(minutes=30)
    assert "Log in again to delete your data" in client.get("/me").text

    back = osu_login(client, 2001, next_url="/me")

    assert back.headers["location"] == "/me"
    page = client.get("/me").text
    assert "Delete all your data?" in page
    assert "Log in again to delete" not in page


def test_a_login_time_in_the_future_does_not_count_as_recent(client, login, csrf, db, clock):
    login(2001)
    token = csrf()
    clock.current -= timedelta(minutes=10)  # the clock jumped back: the login looks like the future

    response = client.post("/me/delete", data=token, follow_redirects=False)

    assert response.headers["location"] == "/auth/login?next=/me"
    assert row(db, "SELECT COUNT(*) FROM users WHERE osu_id = 2001") == (1,)


def test_admins_can_delete_their_own_data_too(client, login, csrf, db):
    login(ADMIN_ID)

    client.post("/me/delete", data=csrf())

    assert row(db, "SELECT COUNT(*) FROM users WHERE osu_id = ?", ADMIN_ID) == (0,)
