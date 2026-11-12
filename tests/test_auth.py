from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.session import safe_next
from tests.support import add_member, add_tournament, code_for, osu_login


def location_query(response) -> dict[str, list[str]]:
    return parse_qs(urlsplit(response.headers["location"]).query)


def test_login_sends_the_user_to_osu_with_state_and_pkce(client, settings):
    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == "https://osu.ppy.sh/oauth/authorize"
    query = location_query(response)
    assert query["client_id"] == [settings.osu_client_id]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["identify"]
    assert query["redirect_uri"] == ["https://tourney.example.com/auth/callback"]
    assert len(query["state"][0]) >= 16
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]


def test_the_redirect_uri_comes_from_public_url_never_from_the_host_header(client):
    response = client.get(
        "/auth/login",
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
        follow_redirects=False,
    )

    assert location_query(response)["redirect_uri"] == ["https://tourney.example.com/auth/callback"]


def test_callback_creates_the_user_and_a_session(client, osu, db):
    osu.add_user(2002, "SamplePlayer", country="FR")

    response = osu_login(client, 2002)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    user = db.execute("SELECT * FROM users WHERE osu_id = 2002").fetchone()
    assert (user["username"], user["country"]) == ("SamplePlayer", "FR")
    assert user["avatar_url"] == "https://a.ppy.sh/2002?1"
    assert user["last_login_at"] == "2026-11-12T12:30:15Z"
    assert user["session_version"] == 1
    assert "SamplePlayer" in client.get("/").text


def test_the_user_token_is_used_once_and_never_stored(client, osu, db):
    osu.add_user(2002, "SamplePlayer")

    osu_login(client, 2002)

    stored = " ".join(str(value) for row in db.execute("SELECT * FROM users") for value in row)
    assert "user-token" not in stored
    assert len(osu.calls("GET", "/me")) == 1
    assert "user-token-2002" not in client.cookies.get("__Host-session", "")


def test_a_returning_user_gets_a_refreshed_profile(client, osu, db, clock):
    osu.add_user(2002, "OldName")
    osu_login(client, 2002)
    osu.profiles[2002]["username"] = "NewName"
    clock.current = clock.current.replace(day=13)

    osu_login(client, 2002)

    rows = db.execute("SELECT username, last_login_at FROM users WHERE osu_id = 2002").fetchall()
    assert [tuple(row) for row in rows] == [("NewName", "2026-11-13T12:30:15Z")]


def test_the_session_cookie_is_http_only_secure_and_same_site_lax(client, osu):
    osu.add_user(2002, "SamplePlayer")

    response = osu_login(client, 2002)

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("__host-session=")
    for attribute in ("httponly", "secure", "samesite=lax", "path=/", "max-age=1209600"):
        assert attribute in cookie
    assert "domain" not in cookie


def test_over_plain_http_the_cookie_is_not_marked_secure(valid_env, clock, osu):
    valid_env["PUBLIC_URL"] = "http://localhost:8080"
    app = create_app(Settings.from_env(valid_env), clock, http_transport=osu.transport)
    osu.add_user(2002, "SamplePlayer")

    with TestClient(app, base_url="http://localhost:8080") as plain_client:
        response = osu_login(plain_client, 2002)

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("session=")
    assert "secure" not in cookie
    assert "httponly" in cookie


def test_a_forged_state_is_rejected_and_nothing_is_created(client, osu, db):
    osu.add_user(2002, "SamplePlayer")
    client.get("/auth/login")

    response = client.get(
        "/auth/callback", params={"code": code_for(2002), "state": "forged"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert "login did not complete" in response.text
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert osu.calls("POST", "/oauth/token") == []
    assert "SamplePlayer" not in client.get("/").text


def test_a_callback_without_a_started_login_is_rejected(client, osu):
    osu.add_user(2002, "SamplePlayer")

    response = client.get(
        "/auth/callback", params={"code": code_for(2002), "state": "x"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert osu.calls("POST", "/oauth/token") == []


def test_a_state_cannot_be_used_twice(client, osu):
    osu.add_user(2002, "SamplePlayer")
    start = client.get("/auth/login", follow_redirects=False)
    state = location_query(start)["state"][0]
    params = {"code": code_for(2002), "state": state}
    assert client.get("/auth/callback", params=params, follow_redirects=False).status_code == 303
    client.cookies.clear()
    client.get("/auth/login")  # a different login attempt is in progress

    replay = client.get("/auth/callback", params=params, follow_redirects=False)

    assert replay.status_code == 400


def test_the_user_declining_on_osu_is_shown_a_retry_page(client):
    client.get("/auth/login")

    response = client.get(
        "/auth/callback", params={"error": "access_denied", "state": "x"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert 'href="/auth/login"' in response.text


def test_a_rejected_code_is_a_failed_login(client, osu, db):
    start = client.get("/auth/login", follow_redirects=False)
    state = location_query(start)["state"][0]

    response = client.get(
        "/auth/callback", params={"code": "bogus", "state": state}, follow_redirects=False
    )

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


@pytest.mark.parametrize("down", ["/oauth/token", "/api/v2/me"])
def test_when_osu_is_down_the_login_fails_with_a_generic_message(client, osu, osu_delays, db, down):
    osu.add_user(2002, "SamplePlayer")
    start = client.get("/auth/login", follow_redirects=False)
    state = location_query(start)["state"][0]
    osu.unavailable_paths.add(down)

    response = client.get(
        "/auth/callback", params={"code": code_for(2002), "state": state}, follow_redirects=False
    )

    assert response.status_code == 502
    assert "osu! did not answer" in response.text
    assert "503" not in response.text
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert "SamplePlayer" not in client.get("/").text


def test_a_token_answer_that_is_not_json_fails_the_login_cleanly(client, osu, db):
    osu.add_user(2002, "SamplePlayer")
    start = client.get("/auth/login", follow_redirects=False)
    state = location_query(start)["state"][0]
    osu.garbage_paths.add("/oauth/token")

    response = client.get(
        "/auth/callback", params={"code": code_for(2002), "state": state}, follow_redirects=False
    )

    assert response.status_code == 502
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("next_url", "expected"),
    [
        ("/t/1/members", "/t/1/members"),
        ("/me", "/me"),
        ("/t/1?stage=2", "/t/1?stage=2"),
        ("https://evil.example/phish", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("evil.example", "/"),
        ("", "/"),
        ("/ok\r\nSet-Cookie: x=1", "/"),
        ("/ok\x00", "/"),
    ],
)
def test_the_next_parameter_only_accepts_paths_on_this_site(client, osu, next_url, expected):
    osu.add_user(2002, "SamplePlayer")

    response = osu_login(client, 2002, next_url=next_url)

    assert response.status_code == 303
    assert response.headers["location"] == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("/x", "/x"), ("//x", "/"), (None, "/"), (5, "/"), ("http://x", "/"), ("/a b", "/a b")],
)
def test_safe_next(raw, expected):
    assert safe_next(raw) == expected


def test_protected_pages_send_visitors_to_the_login_and_back(client, osu, db):
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2002, "player")
    osu.add_user(2002, "SamplePlayer")

    gate = client.get(f"/t/{tournament_id}?stage=3", follow_redirects=False)

    assert gate.status_code == 303
    assert gate.headers["location"] == f"/auth/login?next=%2Ft%2F{tournament_id}%3Fstage%3D3"
    back = osu_login(client, 2002, next_url=f"/t/{tournament_id}?stage=3")
    assert back.headers["location"] == f"/t/{tournament_id}?stage=3"


def test_the_start_page_shows_the_login_for_visitors(client):
    page = client.get("/")

    assert page.status_code == 200
    assert 'href="/auth/login"' in page.text
    assert "Log out" not in page.text


def test_logout_ends_the_session(client, osu, csrf):
    osu.add_user(2002, "SamplePlayer")
    osu_login(client, 2002)
    token = csrf()

    response = client.post("/auth/logout", data=token, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/me", follow_redirects=False).status_code == 303
    assert "SamplePlayer" not in client.get("/").text


def test_a_cookie_for_a_deleted_user_is_treated_as_logged_out(client, osu, db):
    osu.add_user(2002, "SamplePlayer")
    osu_login(client, 2002)
    db.execute("DELETE FROM users WHERE osu_id = 2002")

    assert client.get("/me", follow_redirects=False).status_code == 303


def test_the_tournament_pages_are_not_reachable_with_a_tampered_cookie(client, osu, db):
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2002, "captain")
    osu.add_user(2002, "SamplePlayer")
    osu_login(client, 2002)
    name = next(iter(client.cookies.jar)).name
    client.cookies.set(name, "tampered." + client.cookies[name], domain="tourney.example.com")

    assert client.get(f"/t/{tournament_id}", follow_redirects=False).status_code == 303


def test_the_production_app_has_no_login_shortcut(valid_env, clock):
    with TestClient(create_app(Settings.from_env(valid_env), clock)) as plain:
        assert plain.get("/__test__/login/1001").status_code == 404
