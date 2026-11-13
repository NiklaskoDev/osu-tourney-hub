"""AI predictions are off unless a provider, a model and (for a hosted provider) a key are all set:
no button, no route, no setting, and a clear "off" on the admin page. Every test runs for an empty
setup and for two unfinished ones."""

import logging

import pytest

from app.clients.ai import NullProvider
from app.main import create_app
from tests.matchup_support import CAPTAIN, add_game, build_cup, fill_scores
from tests.support import ADMIN_ID, NoRealNetwork

KEY = "sk-test-0123456789-secret"


@pytest.fixture(
    params=[
        {},
        {"AI_PROVIDER": "anthropic", "AI_API_KEY": KEY},  # no model
        {"AI_PROVIDER": "anthropic", "AI_MODEL": "claude-sonnet-5-5"},  # no key
    ],
    ids=["nothing set", "no model", "no key"],
)
def valid_env(valid_env, request):
    return {**valid_env, **request.param}


@pytest.fixture
def match(db):
    cup = build_cup(db)
    fill_scores(db)
    return cup, add_game(db, cup)


def test_the_provider_is_the_null_provider(app):
    assert isinstance(app.state.ai, NullProvider)
    assert app.state.ai.enabled is False


def test_the_match_page_has_no_ai_section_or_button(client, login, match):
    cup, match_id = match
    login(CAPTAIN)

    page = client.get(f"/t/{cup['id']}/matches/{match_id}").text

    assert "Chance for Alpha Squad" in page  # the statistical estimate is unaffected
    assert "AI estimate" not in page and "Ask the AI" not in page


def test_the_route_does_not_exist_even_for_a_captain(client, login, csrf, db, match, ai):
    cup, match_id = match
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/matches/{match_id}/predict-ai", data=csrf(), follow_redirects=False
    )

    assert response.status_code == 404
    assert ai.requests == []
    assert db.execute("SELECT COUNT(*) FROM predictions WHERE kind = 'ai'").fetchone()[0] == 0


def test_the_admin_page_says_ai_is_off_and_has_no_setting(client, login):
    login(ADMIN_ID, "AdminAmy")

    page = client.get("/admin").text

    assert "AI predictions: off." in page
    assert "share_usernames" not in page
    assert KEY not in page


def test_the_admin_setting_does_not_exist_while_ai_is_off(client, login, csrf):
    login(ADMIN_ID, "AdminAmy")

    response = client.post("/admin/ai-settings", data={**csrf(), "share_usernames": "1"})

    assert response.status_code == 404


def test_a_provider_without_its_model_or_key_is_reported_at_start(
    settings, clock, osu, wybin, ai, caplog
):
    caplog.set_level(logging.WARNING)

    create_app(
        settings,
        clock,
        http_transport=NoRealNetwork(osu, wybin, ai=ai).transport,
        start_workers=False,
    )

    messages = [
        r.getMessage() for r in caplog.records if "AI predictions are off" in r.getMessage()
    ]
    if settings.ai_provider:
        assert len(messages) == 1
        assert ("AI_MODEL" in messages[0]) != ("AI_API_KEY" in messages[0])  # names only
        assert KEY not in caplog.text
    else:
        assert messages == []
