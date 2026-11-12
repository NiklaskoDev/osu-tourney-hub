import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.templating import templates

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
    'class="avail"',
    "result--win",
    "result--loss",
]


@pytest.fixture
def preview_client(valid_env, clock):
    valid_env["STYLEGUIDE_PREVIEW"] = "true"
    with TestClient(create_app(Settings.from_env(valid_env), clock)) as test_client:
        yield test_client


def test_styleguide_is_hidden_by_default(client):
    assert client.get("/styleguide").status_code == 404


def test_styleguide_renders_every_component_when_preview_is_on(preview_client):
    response = preview_client.get("/styleguide")

    assert response.status_code == 200
    for marker in COMPONENT_MARKERS:
        assert marker in response.text, marker


def test_styleguide_page_is_compatible_with_the_csp(preview_client):
    html = preview_client.get("/styleguide").text

    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert not re.search(r"\sstyle=", html), "inline style attribute"
    assert not re.search(r"\son[a-z]+=", html), "inline event handler"
    assert '"allowEval":false' in html
    assert '"includeIndicatorStyles":false' in html


def test_availability_states_are_labelled_with_text(preview_client):
    html = preview_client.get("/styleguide").text

    for state, label in (("yes", "Yes"), ("maybe", "Maybe"), ("no", "No")):
        assert re.search(
            rf'data-state="{state}"[^>]*>\s*<span data-avail-label>{label}</span>', html
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
