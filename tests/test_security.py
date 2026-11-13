"""CSRF protection and the per-IP limiter."""

from datetime import timedelta

import pytest

from app.services.csv_text import MAX_CSV_BYTES
from app.web.security import MAX_REQUEST_BODY_BYTES, SlidingWindowLimiter
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
        "/t/1/scores",
        "/t/1/scores/{score_id}/delete",
        "/t/1/notes",
        "/t/1/notes/{note_id}/delete",
        "/t/1/lineup",
        "/t/1/lineup/copy-suggestion",
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


# --- the request body cap ---

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def form_must_stay_unread(monkeypatch):
    """Fail the test if anything parses the request form."""

    async def unread(self, *args, **kwargs):
        raise AssertionError("the form was read")

    monkeypatch.setattr("starlette.requests.Request.form", unread)


def test_a_body_over_the_cap_is_refused_before_the_form_is_read(
    client, login, form_must_stay_unread
):
    login(2001)

    response = client.post(
        "/me/timezone", content=b"a" * (MAX_REQUEST_BODY_BYTES + 1), headers=FORM
    )

    assert response.status_code == 413  # not 403: the token was never looked at
    assert "larger than 2 MB" in response.text and "at most 1 MB" in response.text


def test_a_body_of_exactly_the_cap_gets_past_the_size_guard(client, login):
    login(2001)

    # Starlette parses at most 1 MB per form field, so the padding is spread over two.
    head, middle = b"timezone=UTC&p1=", b"&p2="
    padding = MAX_REQUEST_BODY_BYTES - len(head) - len(middle)
    body = head + b"a" * (padding // 2) + middle + b"a" * (padding - padding // 2)
    assert len(body) == MAX_REQUEST_BODY_BYTES
    response = client.post("/me/timezone", content=body, headers=FORM)

    assert response.status_code == 403  # on to the CSRF check, which this body fails
    assert CSRF_MESSAGE_FRAGMENT in response.text


def test_the_cap_is_twice_the_csv_limit():
    assert MAX_REQUEST_BODY_BYTES == 2_097_152 == 2 * MAX_CSV_BYTES


def test_a_declared_length_over_the_cap_is_refused_whatever_the_body(
    client, login, form_must_stay_unread
):
    login(2001)

    response = client.post(
        "/me/timezone", content=b"x=1", headers={**FORM, "Content-Length": "2097153"}
    )

    assert response.status_code == 413


@pytest.mark.parametrize("declared", ["abc", "-1", "1.5", "1e3", " "])
def test_a_content_length_that_is_not_a_number_is_a_bad_request(
    client, login, form_must_stay_unread, declared
):
    login(2001)

    response = client.post(
        "/me/timezone", content=b"", headers={**FORM, "Content-Length": declared}
    )

    assert response.status_code == 400
    assert "could not be read" in response.text


def test_a_body_without_a_length_is_refused_with_411(client, login, form_must_stay_unread):
    login(2001)

    def chunks():
        yield b"timezone=UTC"

    response = client.post("/me/timezone", content=chunks(), headers=FORM)

    assert response.status_code == 411
    assert "Send the file again from the page." in response.text


def test_the_cap_applies_to_every_unsafe_method_but_not_to_reads(client, login):
    login(2001)
    big = b"a" * (MAX_REQUEST_BODY_BYTES + 1)

    for method in ("PUT", "PATCH", "DELETE"):
        response = client.request(method, "/me", content=big, headers=FORM)
        assert response.status_code == 413, method

    assert client.get("/me", headers={"Content-Length": "2097153"}).status_code == 200


def test_an_upload_within_the_limits_still_reaches_the_route(client, login, csrf):
    login(2001)

    response = client.post(
        "/me/timezone", data={**csrf(), "timezone": "UTC"}, follow_redirects=False
    )

    assert response.status_code == 303


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
