import re

import pytest

from app.web.templating import templates
from tests.support import ADMIN_ID

COMPONENT_MARKERS = [
    "btn btn--primary",
    "btn btn--secondary",
    "btn btn--danger",
    "btn btn--text",
    "field field--invalid",
    "table-wrap",
    'class="tabs"',
    'class="badge"',
    "alert alert--info",
    "alert alert--success",
    "alert alert--warning",
    "alert alert--error",
    'class="empty"',
    "<dialog",
    "data-countdown",
    'class="pagination"',
    "data-copy=",
    'class="check',
    'method="post"',
    'class="avail"',
    "result--win",
    "result--loss",
]


@pytest.fixture
def admin_page(client, login) -> str:
    login(ADMIN_ID)
    response = client.get("/styleguide")
    assert response.status_code == 200
    return response.text


def test_styleguide_sends_anonymous_visitors_to_the_login(client):
    response = client.get("/styleguide", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=%2Fstyleguide"


def test_styleguide_is_forbidden_for_a_non_admin(client, login):
    login(5555)

    assert client.get("/styleguide").status_code == 403


def test_styleguide_renders_every_component_for_an_admin(admin_page):
    for marker in COMPONENT_MARKERS:
        assert marker in admin_page, marker


def test_styleguide_page_is_compatible_with_the_csp(admin_page):
    assert not re.search(r"<script(?![^>]*\bsrc=)", admin_page), "inline script"
    assert not re.search(r"\sstyle=", admin_page), "inline style attribute"
    assert not re.search(r"\son[a-z]+=", admin_page), "inline event handler"
    assert '"allowEval":false' in admin_page
    assert '"includeIndicatorStyles":false' in admin_page


def test_availability_states_are_labelled_with_text(admin_page):
    for state, label in (("yes", "Yes"), ("maybe", "Maybe"), ("no", "No")):
        assert re.search(
            rf'data-state="{state}"[^>]*>\s*<span data-avail-label>{label}</span>', admin_page
        )


def test_static_assets_are_served(client):
    assert "--accent" in client.get("/static/css/tokens.css").text
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/htmx.min.js").status_code == 200
    assert client.get("/static/js/htmx-ext-sse.js").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert "<symbol" in client.get("/static/icons/sprite.svg").text


def test_templates_escape_user_text():
    html = templates.env.from_string(
        '{% from "components/field.html" import field %}{{ field("name", "Name", value=v) }}'
    ).render(v='"><script>alert(1)</script>')

    assert "<script>" not in html
    assert "&lt;script&gt;" in html or "&#34;&gt;&lt;script" in html
