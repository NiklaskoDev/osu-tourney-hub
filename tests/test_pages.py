"""Properties every rendered page must have: error handling, CSP compatibility, security headers."""

import re

import pytest

from tests.support import ADMIN_ID, add_member, add_tournament


@pytest.fixture
def seeded(db) -> int:
    tournament_id = add_tournament(db)
    add_member(db, tournament_id, 2001, "captain")
    add_member(db, tournament_id, 2002, "player")
    return tournament_id


def page_paths(tournament_id: int) -> list[str]:
    return ["/", "/me", f"/t/{tournament_id}", f"/t/{tournament_id}/members", "/styleguide"]


def test_every_page_is_compatible_with_the_csp(client, login, seeded):
    visitor = client.get("/").text  # the login screen, seen before anyone logs in
    assert "Log in with osu!" in visitor
    login(ADMIN_ID)
    pages = [visitor] + [client.get(path).text for path in page_paths(seeded)]
    pages.append(client.get("/t/9999").text)  # the error page

    for html in pages:
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
        assert not re.search(r"\sstyle=", html), "inline style attribute"
        assert not re.search(r"\son[a-z]+=", html), "inline event handler"
        assert "<style" not in html


def test_headings_start_with_one_h1(client, login, seeded):
    login(ADMIN_ID)

    for path in page_paths(seeded):
        assert len(re.findall(r"<h1[ >]", client.get(path).text)) == 1, path


def test_every_form_control_has_a_label(client, login, seeded):
    login(2001)

    for path in ("/me", f"/t/{seeded}/members"):
        html = client.get(path).text
        for control in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
            if 'type="hidden"' in control:
                continue
            control_id = re.search(r'\bid="([^"]+)"', control)
            assert control_id, control
            assert f'for="{control_id.group(1)}"' in html, (path, control)


def test_error_pages_are_html_carry_security_headers_and_hide_details(client, login):
    login(2001)

    response = client.get("/t/9999")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Content-Security-Policy" in response.headers
    assert "Traceback" not in response.text
    assert "Page not found" in response.text


def test_a_wrong_method_gets_a_plain_html_error(client, login):
    login(2001)

    response = client.post("/healthz", data={})

    assert response.status_code in (403, 405)
    assert response.headers["content-type"].startswith("text/html")


def test_errors_say_what_happened_in_the_users_words(client, login, csrf, seeded):
    login(2002)

    forbidden = client.post(f"/t/{seeded}/members", data={"csrf_token": "x"})
    assert forbidden.status_code == 403
    assert "out of date" in forbidden.text

    login(2002)
    refused = client.post(f"/t/{seeded}/members", data={**csrf(), "osu_id": "5"})
    assert refused.status_code == 403
    assert "You do not have permission to do this." in refused.text


def test_pages_escape_names_from_osu(client, login, db, seeded):
    db.execute(
        "UPDATE users SET username = ? WHERE osu_id = 2002", ('<script>alert(1)</script>"x',)
    )
    login(2001)

    html = client.get(f"/t/{seeded}/members").text

    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_the_login_screen_is_the_only_page_a_visitor_can_open(client, seeded):
    for path in ("/me", "/me/export", f"/t/{seeded}", f"/t/{seeded}/members", "/styleguide"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/auth/login?next="), path
