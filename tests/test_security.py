"""CSRF protection and the per-IP limiter."""

from datetime import timedelta

import pytest

from app.web.security import SlidingWindowLimiter
from tests.support import add_member, add_tournament

CSRF_MESSAGE_FRAGMENT = "out of date"


def post_routes(app) -> list[str]:
    """Every POST path the app serves, with sample values for path parameters.

    The OpenAPI schema is the public list of routes; the page itself is not served in
    production (openapi_url is off) but the app can still build it.
    """
    paths = []
    for path, operations in app.openapi()["paths"].items():
        if "post" in operations:
            for name in ("tid", "osu_id"):
                path = path.replace("{" + name + "}", "1")
            paths.append(path)
    return sorted(paths)


def test_the_route_table_has_post_routes_to_check(app):
    paths = post_routes(app)

    for expected in (
        "/auth/logout",
        "/me/timezone",
        "/me/logout-all",
        "/me/delete",
        "/t/1/members",
        "/t/1/members/1",
        "/t/1/members/1/remove",
    ):
        assert expected in paths


def test_every_post_route_refuses_a_missing_token(app, client, login, db):
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2001, "captain")
    login(2001)

    for path in post_routes(app):
        response = client.post(path, data={"role": "guest", "timezone": "UTC"})

        assert response.status_code == 403, path
        assert CSRF_MESSAGE_FRAGMENT in response.text, path


def test_every_post_route_refuses_a_wrong_token(app, client, login, db):
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2001, "captain")
    login(2001)

    for path in post_routes(app):
        wrong_field = client.post(path, data={"csrf_token": "wrong"})
        wrong_header = client.post(path, headers={"X-CSRF-Token": "wrong"})

        assert wrong_field.status_code == 403, path
        assert wrong_header.status_code == 403, path


def test_every_post_route_refuses_anonymous_visitors(app, client):
    for path in post_routes(app):
        assert client.post(path, data={"csrf_token": "anything"}).status_code == 403, path


def test_a_token_from_another_session_is_refused(client, login, csrf, db):
    login(2001)
    stolen = csrf()
    login(2002)

    response = client.post("/me/timezone", data={**stolen, "timezone": "UTC"})

    assert response.status_code == 403
    assert db.execute("SELECT timezone FROM users WHERE osu_id = 2002").fetchone()[0] is None


def test_the_form_field_token_is_accepted(client, login, csrf):
    login(2001)

    response = client.post(
        "/me/timezone", data={**csrf(), "timezone": "UTC"}, follow_redirects=False
    )

    assert response.status_code == 303


def test_the_header_token_is_accepted_for_htmx_requests(client, login, csrf):
    login(2001)

    response = client.post(
        "/me/timezone",
        data={"timezone": "UTC"},
        headers={"X-CSRF-Token": csrf()["csrf_token"]},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_pages_with_a_logged_in_user_carry_the_token_for_htmx(client, login, csrf):
    login(2001)

    page = client.get("/me").text

    assert f'hx-headers=\'{{"X-CSRF-Token": "{csrf()["csrf_token"]}"}}\'' in page


def test_get_requests_need_no_token(client, login):
    login(2001)

    assert client.get("/me").status_code == 200


def test_the_token_changes_with_every_login(client, login, csrf):
    login(2001)
    first = csrf()
    login(2001)

    assert csrf() != first


class TestSlidingWindowLimiter:
    def make(self, clock, limit=3, seconds=60):
        return SlidingWindowLimiter(limit, timedelta(seconds=seconds), clock)

    def test_allows_up_to_the_limit_then_refuses(self, clock):
        limiter = self.make(clock)

        assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]

    def test_keys_are_counted_separately(self, clock):
        limiter = self.make(clock, limit=1)

        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        assert limiter.allow("a") is False

    def test_the_window_slides(self, clock):
        limiter = self.make(clock, limit=2)
        limiter.allow("a")
        clock.current += timedelta(seconds=30)
        limiter.allow("a")
        assert limiter.allow("a") is False

        clock.current += timedelta(seconds=30)  # the first hit is now a minute old
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False

    def test_refused_calls_do_not_extend_the_block(self, clock):
        limiter = self.make(clock, limit=1)
        limiter.allow("a")
        for _ in range(10):
            clock.current += timedelta(seconds=5)
            limiter.allow("a")

        clock.current += timedelta(seconds=10)  # 60 s after the only counted hit

        assert limiter.allow("a") is True

    def test_retry_after_counts_down_to_the_oldest_hit_leaving(self, clock):
        limiter = self.make(clock, limit=1)
        limiter.allow("a")
        clock.current += timedelta(seconds=20)

        assert limiter.retry_after_seconds("a") == 41
        assert limiter.retry_after_seconds("unknown") == 0

    def test_old_keys_are_swept_when_too_many_are_tracked(self, clock):
        limiter = self.make(clock)
        limiter.MAX_TRACKED_KEYS = 5
        for index in range(5):
            limiter.allow(f"old-{index}")
        clock.current += timedelta(seconds=120)

        limiter.allow("new-1")
        limiter.allow("new-2")

        assert sorted(limiter._hits) == ["new-1", "new-2"]


def test_the_auth_endpoints_are_limited_per_client_address(client, clock):
    statuses = [client.get("/auth/login", follow_redirects=False).status_code for _ in range(21)]

    assert statuses[:20] == [302] * 20
    assert statuses[20] == 429


def test_the_limit_response_says_when_to_retry_and_clears_with_time(client, clock):
    for _ in range(20):
        client.get("/auth/login", follow_redirects=False)

    blocked = client.get("/auth/login", follow_redirects=False)
    assert blocked.headers["Retry-After"] == "61"
    assert "Too many" in blocked.text

    clock.current += timedelta(seconds=61)
    assert client.get("/auth/login", follow_redirects=False).status_code == 302


def test_other_pages_are_not_subject_to_the_auth_limit(client, clock):
    for _ in range(25):
        assert client.get("/").status_code == 200


@pytest.mark.parametrize("path", ["/auth/callback", "/auth/login"])
def test_the_callback_is_limited_too(client, path):
    for _ in range(20):
        client.get(path, follow_redirects=False)

    assert client.get(path, follow_redirects=False).status_code == 429
