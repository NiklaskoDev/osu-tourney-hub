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


def test_availability_cells_expose_whose_slot_and_which_state_to_assistive_tech(admin_page):
    assert 'aria-label="Mon 16 Nov 18:00: Yes"' in admin_page
    assert 'aria-label="Mon 16 Nov 18:30: Maybe"' in admin_page
    assert 'aria-label="Mon 16 Nov 19:00: No"' in admin_page


def test_static_assets_are_served(client):
    assert "--accent" in client.get("/static/css/tokens.css").text
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/htmx.min.js").status_code == 200
    assert client.get("/static/js/htmx-ext-sse.js").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/js/availability.js").status_code == 200
    assert "<symbol" in client.get("/static/icons/sprite.svg").text


def test_templates_escape_user_text():
    html = templates.env.from_string(
        '{% from "components/field.html" import field %}{{ field("name", "Name", value=v) }}'
    ).render(v='"><script>alert(1)</script>')

    assert "<script>" not in html
    assert "&lt;script&gt;" in html or "&#34;&gt;&lt;script" in html


# --- the design revamp: tokens, new components and their sections ---

NEW_TOKENS = [
    "--surface-3",
    "--border-strong",
    "--accent-soft",
    "--avail-yes",
    "--avail-maybe",
    "--avail-no",
    "--control-h",
    "--sidebar-w",
    "--topbar-h",
    "--gutter",
    "--max-form",
    "--radius-lg",
    "--weight-medium",
]


def render_macro(source: str, **values) -> str:
    return templates.env.from_string(source).render(**values)


def test_the_design_tokens_exist_in_both_colour_schemes(client):
    css = client.get("/static/css/tokens.css").text

    for token in NEW_TOKENS:
        assert f"{token}:" in css, token
    dark = css.split("prefers-color-scheme: dark")[1]
    for token in ("--surface-3", "--border-strong", "--avail-no"):
        assert f"{token}:" in dark
    assert "--control-h: 44px" in css.split("pointer: coarse")[1]


def test_an_inline_field_puts_its_label_beside_the_control():
    html = render_macro(
        '{% from "components/field.html" import field %}'
        '{{ field("a", "A", inline=true) }}{{ field("b", "B") }}'
    )

    assert html.count("field--inline") == 1
    assert html.index("field--inline") < html.index('for="b"')


def test_a_table_only_scrolls_in_its_own_box_when_asked():
    source = (
        '{% from "components/table.html" import table %}'
        '{% call table("Plain") %}<tbody></tbody>{% endcall %}'
        '{% call table("Wide", scroll=true) %}<tbody></tbody>{% endcall %}'
    )

    html = render_macro(source)

    assert html.count("table-wrap--scroll") == 1
    assert html.index('aria-label="Plain"') < html.index("table-wrap--scroll")


def test_a_switch_is_a_real_checkbox_with_two_labels():
    html = render_macro(
        '{% from "components/switch.html" import switch %}'
        '{{ switch("tz", "UTC", "Local", checked=true, value="local", id="s") }}'
    )

    assert 'type="checkbox" role="switch"' in html and 'name="tz" value="local" checked' in html
    assert html.index("UTC") < html.index("switch__track") < html.index("Local")
    assert 'aria-hidden="true"' in html  # the track is decoration; the input carries the state


def test_the_styleguide_shows_the_new_sections(admin_page):
    for heading in (
        "Toolbar and switch",
        "Availability timeline",
        "Upcoming matches",
        "Quick score bar",
    ):
        assert heading in admin_page, heading
    for marker in (
        "swatch__chip--surface-3",
        "swatch__chip--avail-maybe",
        'class="timeline"',
        "upcoming__item--next",
        "quick-score",
        'role="switch"',
        "Team B (sample)",
    ):
        assert marker in admin_page, marker
    assert admin_page.count('role="switch"') == 3 and admin_page.count(" disabled") >= 8


def test_the_styleguide_timeline_is_built_by_the_real_builder(admin_page):
    assert "Yes 18:00-23:00, Maybe 23:00-24:00, No otherwise" in admin_page
    assert "is-unfilled" in admin_page and "vs Team B (sample)" in admin_page


def autosubmit_forms(html: str) -> list[str]:
    return re.findall(r"<form\b[^>]*data-autosubmit[^>]*>.*?</form>", html, re.S)


def test_every_filter_form_applies_on_change_without_browser_validation(
    client, login, db, cup_with_filters
):
    """requestSubmit() runs browser validation first, so a form that does not say novalidate, or
    holds a required select left on its empty choice, would silently send nothing."""
    login(2002)
    tid = cup_with_filters
    pages = {
        "lobbies": client.get(f"/t/{tid}/lobbies").text,
        "lineup": client.get(f"/t/{tid}/lineup").text,
        "availability": client.get(f"/t/{tid}/availability").text,
        "availability edit": client.get(f"/t/{tid}/availability?edit=1").text,
    }

    for name, html in pages.items():
        forms = autosubmit_forms(html)
        assert forms, f"{name}: no auto-applying filter form"
        for form in forms:
            opening = form.split(">", 1)[0]
            assert 'method="get"' in opening and "novalidate" in opening, name
            assert "<noscript>" in form and 'type="submit"' in form, name
            assert not re.search(r"<(select|input)[^>]*\srequired", form), name


def test_the_lineup_match_select_is_not_required_either(client, login, db, cup_with_filters):
    from tests.support import add_match, add_team

    tid = cup_with_filters
    stage = db.execute("SELECT id FROM stages WHERE tournament_id = ?", (tid,)).fetchone()[0]
    ours = add_team(db, tid, "Ours", [(2002, "p")], ours=True)
    add_match(db, tid, ours, add_team(db, tid, "Them"), "2026-11-20T19:00:00Z", stage)
    login(2002)

    html = client.get(f"/t/{tid}/lineup").text

    form = autosubmit_forms(html)[0]
    assert 'name="match"' in form and "novalidate" in form.split(">", 1)[0]
    assert not re.search(r"<select[^>]*\srequired", form)
    assert "(optional)" not in form


def test_the_script_submits_only_get_forms_that_ask_for_it(client):
    script = client.get("/static/js/app.js").text

    assert "data-autosubmit" in script and "requestSubmit" in script
    assert "data-countdown-prefix" in script
    assert 'form.method !== "get"' in script


@pytest.fixture
def cup_with_filters(db):
    from tests.support import add_member, add_pool_map, add_stage, add_tournament

    tid = add_tournament(db, "Filter Cup")
    add_member(db, tid, 2002, "player")
    add_pool_map(db, add_stage(db, tid), "NM1")
    return tid
