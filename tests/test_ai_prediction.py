"""The AI estimate with a provider configured: what is sent, what is stored and shown, who may ask,
the per-person limit, and the admin setting that decides whether names leave the server."""

import json
from datetime import timedelta

import httpx
import pytest

from app.config import Settings
from app.repos import app_settings as app_settings_repo
from app.repos import matches as matches_repo
from app.repos import tournaments as tournaments_repo
from app.services import ai_prediction, matchup
from tests.matchup_support import CAPTAIN, MATE, add_game, build_cup, fill_scores
from tests.support import ADMIN_ID, add_member, anthropic_reply

KEY = "sk-test-0123456789-secret"
OTHER_CAPTAIN = 2005
STRANGER = 2009
GOOD = {
    "winner": "A",
    "win_percent": 70,
    "bans": {"A": ["NM2"], "B": ["HD1"]},
    "picks": [{"team": "A", "slot": "NM1"}, {"team": "B", "slot": "TB"}],
    "reasoning": "Alpha ahead on the nomod maps, so they win in three.",
}
ASK = "Ask the AI for an estimate"


@pytest.fixture
def valid_env(valid_env):
    return {
        **valid_env,
        "AI_PROVIDER": "anthropic",
        "AI_API_KEY": KEY,
        "AI_MODEL": "claude-sonnet-5-5",
    }


@pytest.fixture
def cup(db):
    return build_cup(db)


@pytest.fixture
def match_id(db, cup):
    fill_scores(db)
    return add_game(db, cup)


def url(cup, match, tail=""):
    return f"/t/{cup['id']}/matches/{match}{tail}"


def ask(client, token, cup, match, **kwargs):
    return client.post(url(cup, match, "/predict-ai"), data=token, follow_redirects=False, **kwargs)


def stored(db):
    rows = db.execute(
        "SELECT kind, win_prob_team1, expected_score, payload, model_version FROM predictions "
        "WHERE kind = 'ai'"
    ).fetchall()
    return [tuple(row) for row in rows]


def sent_text(ai):
    """Everything the provider was sent, as one string."""
    return "".join(request.content.decode() for request in ai.requests)


# --- asking ------------------------------------------------------------------------------------


def test_a_captain_asks_and_the_estimate_is_stored_and_shown(
    client, login, csrf, db, cup, match_id, ai
):
    login(CAPTAIN)
    page = client.get(url(cup, match_id)).text
    assert ASK in page and "No AI estimate for this match yet." in page
    ai.replies.append(anthropic_reply(GOOD))

    response = ask(client, csrf(), cup, match_id)

    assert response.status_code == 303
    assert response.headers["location"] == url(cup, match_id)
    ((kind, team1_chance, score, payload, model),) = stored(db)
    assert kind == "ai" and team1_chance == pytest.approx(0.7) and score is None
    assert model == "anthropic:claude-sonnet-5-5"
    assert json.loads(payload) == {
        "winner_team_id": cup["ours"],
        "win_percent": 70,
        "bans": {str(cup["ours"]): ["NM2"], str(cup["rivals"]): ["HD1"]},
        "picks": [
            {"team_id": cup["ours"], "slot": "NM1"},
            {"team_id": cup["rivals"], "slot": "TB"},
        ],
        "reasoning": GOOD["reasoning"],
    }

    shown = client.get(url(cup, match_id)).text
    assert "AI estimate saved." in shown
    assert 'id="ai-heading">AI estimate</h2>' in shown
    assert "Chance for Alpha Squad" in shown
    assert GOOD["reasoning"] in shown
    assert "anthropic:claude-sonnet-5-5" in shown
    assert "Ask the AI again" in shown and ASK not in shown
    assert KEY not in shown


def test_the_statistical_estimate_stays_beside_the_ai_one(
    client, login, csrf, db, cup, match_id, ai
):
    login(CAPTAIN)
    ai.replies.append(anthropic_reply(GOOD))

    client.get(url(cup, match_id))
    ask(client, csrf(), cup, match_id)

    kinds = [row[0] for row in db.execute("SELECT kind FROM predictions ORDER BY id")]
    assert sorted(kinds) == ["ai", "stat"]
    page = client.get(url(cup, match_id)).text
    assert page.index("Most likely score") < page.index("AI estimate</h2>")


def test_the_estimate_is_kept_from_team_ones_side_and_shown_from_the_pages(
    client, login, csrf, db, cup, ai
):
    fill_scores(db)
    match = add_game(db, cup, ours_first=False)
    login(CAPTAIN)
    ai.replies.append(anthropic_reply({**GOOD, "winner": "B", "win_percent": 80}))

    ask(client, csrf(), cup, match)

    ((_, team1_chance, *_),) = stored(db)
    assert team1_chance == pytest.approx(0.8)  # B is the rivals, who are team 1 here
    page = client.get(url(cup, match)).text
    assert "Chance for Alpha Squad" in page
    assert '<span class="result--loss">L</span> <span class="num">20%' in page
    assert "Rival Crew <span" in page  # the expected winner


def test_a_player_has_no_button_and_cannot_ask(client, login, csrf, db, cup, match_id, ai):
    login(MATE)

    assert ASK not in client.get(url(cup, match_id)).text
    assert ask(client, csrf(), cup, match_id).status_code == 403
    assert ai.requests == [] and stored(db) == []


def test_a_stored_estimate_is_visible_to_a_player(client, login, csrf, db, cup, match_id, ai):
    login(CAPTAIN)
    ai.replies.append(anthropic_reply(GOOD))
    ask(client, csrf(), cup, match_id)

    login(MATE)
    page = client.get(url(cup, match_id)).text

    assert GOOD["reasoning"] in page and "Ask the AI" not in page


def test_a_stranger_a_foreign_match_and_a_missing_csrf_token_are_refused(
    client, login, csrf, db, cup, match_id, ai
):
    other = db.execute(
        "INSERT INTO tournaments (name, gamemode, is_solo, status, source, created_at) "
        "VALUES ('Other', 0, 0, 'active', 'manual', '2026-01-01T00:00:00Z')"
    ).lastrowid
    add_member(db, other, STRANGER, "captain")

    login(STRANGER)
    assert ask(client, csrf(), cup, match_id).status_code == 404

    login(CAPTAIN)
    assert ask(client, {}, cup, match_id).status_code == 403
    assert client.post(f"/t/{other}/matches/{match_id}/predict-ai", data=csrf()).status_code == 404
    assert ask(client, csrf(), cup, match_id + 100).status_code == 404
    assert ai.requests == [] and stored(db) == []


def test_an_archived_tournament_takes_no_new_estimate(client, login, csrf, db, cup, match_id, ai):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    assert ASK not in client.get(url(cup, match_id)).text
    assert ask(client, csrf(), cup, match_id).status_code == 409
    assert ai.requests == []


def test_a_match_without_a_published_pool_has_nothing_to_estimate(
    client, login, csrf, db, cup, match_id, ai
):
    db.execute("UPDATE stages SET pool_released = 0 WHERE id = ?", (cup["stage"],))
    login(CAPTAIN)

    assert ASK not in client.get(url(cup, match_id)).text
    refused = ask(client, csrf(), cup, match_id)
    assert refused.status_code == 409
    assert "pool" in refused.text
    assert ai.requests == [] and stored(db) == []


def test_an_ask_with_nothing_to_estimate_does_not_use_up_the_limit(
    client, login, csrf, db, cup, match_id, ai
):
    db.execute("UPDATE stages SET pool_released = 0 WHERE id = ?", (cup["stage"],))
    login(CAPTAIN)
    token = csrf()

    assert [ask(client, token, cup, match_id).status_code for _ in range(6)] == [409] * 6

    db.execute("UPDATE stages SET pool_released = 1 WHERE id = ?", (cup["stage"],))
    ai.replies.extend([anthropic_reply(GOOD)] * 5)
    assert [ask(client, token, cup, match_id).status_code for _ in range(5)] == [303] * 5
    assert len(ai.requests) == 5


# --- failures ----------------------------------------------------------------------------------


def test_a_malformed_answer_shows_a_generic_error_and_stores_nothing(
    client, login, csrf, db, cup, match_id, ai
):
    login(CAPTAIN)
    bad = anthropic_reply({**GOOD, "winner": "C"})
    ai.replies.extend([bad, bad])

    response = ask(client, csrf(), cup, match_id)

    assert response.status_code == 502
    assert "The AI service could not give a usable estimate. Nothing was saved." in response.text
    assert KEY not in response.text
    assert stored(db) == []
    assert len(ai.requests) == 2  # one retry, no more
    assert ASK in response.text  # the page is still usable and offers another try


def test_a_provider_that_refuses_stores_nothing_and_does_not_leak_its_reply(
    client, login, csrf, db, cup, match_id, ai
):
    login(CAPTAIN)
    ai.replies.append(httpx.Response(401, text=f"invalid x-api-key {KEY}"))

    response = ask(client, csrf(), cup, match_id)

    assert response.status_code == 502
    assert KEY not in response.text and "x-api-key" not in response.text
    assert stored(db) == [] and len(ai.requests) == 1


def test_a_failed_ask_keeps_the_estimate_from_before(client, login, csrf, db, cup, match_id, ai):
    login(CAPTAIN)
    ai.replies.extend(
        [
            anthropic_reply(GOOD),
            httpx.Response(401),
        ]
    )
    ask(client, csrf(), cup, match_id)

    ask(client, csrf(), cup, match_id)

    assert len(stored(db)) == 1
    assert GOOD["reasoning"] in client.get(url(cup, match_id)).text


# --- the limit ---------------------------------------------------------------------------------


def test_five_asks_an_hour_per_person_then_the_sixth_waits(
    client, login, csrf, db, cup, match_id, ai, clock
):
    add_member(db, cup["id"], OTHER_CAPTAIN, "captain")
    ai.replies.extend([anthropic_reply(GOOD)] * 8)
    login(CAPTAIN)
    token = csrf()

    for _ in range(5):
        assert ask(client, token, cup, match_id).status_code == 303
    sixth = ask(client, token, cup, match_id)

    assert sixth.status_code == 429
    assert int(sixth.headers["Retry-After"]) > 0
    assert len(ai.requests) == 5 and len(stored(db)) == 5

    login(OTHER_CAPTAIN)  # the limit is per person
    assert ask(client, csrf(), cup, match_id).status_code == 303

    clock.current += timedelta(hours=1, seconds=1)
    login(CAPTAIN)
    assert ask(client, csrf(), cup, match_id).status_code == 303


def test_a_failed_ask_counts_against_the_limit(client, login, csrf, db, cup, match_id, ai):
    login(CAPTAIN)
    token = csrf()
    ai.replies.extend([httpx.Response(401)] * 6)

    statuses = [ask(client, token, cup, match_id).status_code for _ in range(6)]

    assert statuses == [502] * 5 + [429]
    assert len(ai.requests) == 5


# --- what is sent ------------------------------------------------------------------------------


def context_for(db, cup, match_id, share):
    tournament = tournaments_repo.get(db, cup["id"])
    view = matchup.build_matchup(db, tournament, matches_repo.get(db, match_id))
    return ai_prediction.build_context(db, tournament, view, share)


def sent_context(ai) -> str:
    """The prompt data of the first request, as the text the provider received."""
    return json.loads(ai.requests[0].content)["messages"][0]["content"]


def test_no_name_leaves_the_server_unless_an_admin_allows_it(
    client, login, csrf, db, cup, match_id, ai
):
    login(CAPTAIN)
    ai.replies.append(anthropic_reply(GOOD))

    ask(client, csrf(), cup, match_id)

    text = sent_context(ai)
    for name in ("Cap", "Mate", "Sub", "Guest", "RivalA", "RivalB", "Alpha", "Rival Crew"):
        assert name not in text, name
    assert KEY not in sent_text(ai)
    context = json.loads(text)
    assert context["teams"] == {"A": "Team A", "B": "Team B"}
    assert [p["name"] for p in context["players"]["A"]] == ["Player A", "Player B"]
    assert [p["name"] for p in context["players"]["B"]] == ["Player C", "Player D"]


def test_sharing_names_sends_them(client, login, csrf, db, cup, match_id, ai):
    app_settings_repo.put(db, ai_prediction.SHARE_USERNAMES_KEY, "1")
    login(CAPTAIN)
    ai.replies.append(anthropic_reply(GOOD))

    ask(client, csrf(), cup, match_id)

    context = json.loads(sent_context(ai))
    assert context["teams"] == {"A": "Alpha Squad", "B": "Rival Crew"}
    assert [p["name"] for p in context["players"]["A"]] == ["Cap", "Mate"]
    assert [p["name"] for p in context["players"]["B"]] == ["RivalA", "RivalB"]


def test_the_context_holds_the_pool_the_scores_and_the_models_output(db, cup, match_id):
    context = context_for(db, cup, match_id, share=False)

    assert context.slots == {"NM1", "NM2", "HD1", "TB"}
    data = context.data
    assert set(data) == {"stage", "teams", "maps", "players", "stat_model", "habits"}
    assert data["stage"] == {
        "best_of": 3,
        "bans_per_team": 1,
        "protects_per_team": 0,
        "players_per_map": 2,
        "is_qualifier": False,
    }
    assert [m["slot"] for m in data["maps"]] == ["NM1", "NM2", "HD1", "TB"]
    hd1 = next(m for m in data["maps"] if m["slot"] == "HD1")
    assert hd1["required_mods"] == "HD" and hd1["tiebreaker"] is False
    assert hd1["expected_score"]["A"] == {"score": 2_000_000, "estimated": False}
    assert hd1["expected_score"]["B"] == {"score": 1_600_000, "estimated": False}
    assert hd1["stat_chance_A"] > 0.5
    assert set(hd1) == {
        "slot",
        "bracket",
        "required_mods",
        "freemod",
        "tiebreaker",
        "stars",
        "expected_score",
        "stat_chance_A",
    }  # no title, artist, mapper or beatmap id
    assert data["players"]["A"][0]["best_scores"] == {
        "NM1": 1_000_000,
        "NM2": 1_000_000,
        "HD1": 1_000_000,
        "TB": 1_000_000,
    }
    assert data["stat_model"]["chance_A"] > 0.5
    assert {o["first_pick"] for o in data["stat_model"]["orders"]} == {"A", "B"}
    assert data["habits"] == {"A": None, "B": None}


def test_the_format_sentence_is_sent_only_when_names_may_be_sent(db, cup, match_id):
    private = context_for(db, cup, match_id, share=False).data["stage"]
    shared = context_for(db, cup, match_id, share=True).data["stage"]

    assert "format" not in private
    assert (private["best_of"], private["bans_per_team"], private["protects_per_team"]) == (3, 1, 0)
    assert shared == {**private, "format": "Best of 3, 1 ban"}


def test_a_stage_without_a_format_is_sent_as_not_set_when_names_may_be_sent(db, cup, match_id):
    db.execute(
        "UPDATE stages SET best_of = NULL, bans = NULL, protects = NULL WHERE id = ?",
        (cup["stage"],),
    )

    assert context_for(db, cup, match_id, share=True).data["stage"]["format"] == "not set"


def test_a_benched_player_is_not_sent_for_our_team(db, cup, match_id):
    players = context_for(db, cup, match_id, share=True).data["players"]["A"]

    assert [p["name"] for p in players] == ["Cap", "Mate"]  # not the substitute or the guest


def test_players_are_ordered_by_username_never_by_osu_id(db, cup, match_id):
    db.execute("UPDATE team_players SET username = 'Zed' WHERE osu_id = 3001")
    db.execute("UPDATE team_players SET username = 'Abe' WHERE osu_id = 3002")  # higher id, first

    shared = context_for(db, cup, match_id, share=True).data["players"]["B"]
    private = context_for(db, cup, match_id, share=False).data["players"]["B"]

    assert [p["name"] for p in shared] == ["Abe", "Zed"]
    assert [p["name"] for p in private] == ["Player C", "Player D"]
    # Player C is Abe: the labels follow the same order, so they say nothing about ids.
    assert context_for(db, cup, match_id, share=False).data["players"]["B"] == private


def test_the_teams_are_a_and_b_in_the_pages_orientation(db, cup):
    fill_scores(db)
    match = add_game(db, cup, ours_first=False)

    data = context_for(db, cup, match, share=True).data

    expected = data["maps"][0]["expected_score"]
    assert data["teams"] == {
        "A": "Alpha Squad",
        "B": "Rival Crew",
    }  # ours on the left, as on the page
    assert expected["A"]["score"] > expected["B"]["score"]


def test_a_match_without_teams_cannot_be_sent(db, cup):
    undecided = db.execute(
        "INSERT INTO matches (tournament_id, stage_id, team1_id, status, updated_at) "
        "VALUES (?, ?, ?, 'scheduled', '2026-11-01T00:00:00Z')",
        (cup["id"], cup["stage"], cup["ours"]),
    ).lastrowid

    with pytest.raises(ai_prediction.CannotPredict):
        context_for(db, cup, undecided, share=False)


def test_more_players_than_letters_get_numbers():
    labels = ai_prediction._player_labels(list(range(100, 130)))

    assert labels[100] == "Player A" and labels[125] == "Player Z"
    assert labels[126] == "Player 27" and labels[129] == "Player 30"
    assert len(set(labels.values())) == 30


# --- the admin console -------------------------------------------------------------------------


def test_the_admin_page_shows_that_ai_is_on_and_never_the_key(client, login, csrf):
    login(ADMIN_ID, "AdminAmy")

    page = client.get("/admin").text

    assert "AI predictions: on." in page
    assert "claude-sonnet-5-5" in page and "anthropic" in page
    assert KEY not in page


def test_the_admin_template_gets_the_ai_facts_and_no_key(client, login, monkeypatch):
    from app.web.routes import admin

    seen = {}
    real = admin.render

    def capture(request, template, context=None, **kwargs):
        seen.update(context or {})
        return real(request, template, context, **kwargs)

    monkeypatch.setattr(admin, "render", capture)
    login(ADMIN_ID, "AdminAmy")

    client.get("/admin")

    assert seen["ai"] == {
        "enabled": True,
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "missing": [],
    }
    assert not any(isinstance(value, Settings) for value in seen.values())
    assert KEY not in repr(seen)


def test_the_admin_decides_whether_names_are_shared(client, login, csrf, db):
    login(ADMIN_ID, "AdminAmy")
    assert ai_prediction.share_usernames(db) is False
    assert 'name="share_usernames" value="1" checked' not in client.get("/admin").text

    on = client.post("/admin/ai-settings", data={**csrf(), "share_usernames": "1"})
    assert on.status_code == 200  # followed to /admin
    assert ai_prediction.share_usernames(db) is True
    assert 'name="share_usernames" value="1" checked' in client.get("/admin").text

    client.post("/admin/ai-settings", data=csrf())  # an unticked box sends nothing
    assert ai_prediction.share_usernames(db) is False


def test_only_an_admin_changes_the_setting(client, login, csrf, db):
    login(CAPTAIN)

    refused = client.post("/admin/ai-settings", data={**csrf(), "share_usernames": "1"})

    assert refused.status_code == 403
    assert ai_prediction.share_usernames(db) is False


# --- reading back ------------------------------------------------------------------------------


def test_the_newest_estimate_is_the_one_shown(client, login, csrf, db, cup, match_id, clock, ai):
    login(CAPTAIN)
    first = {**GOOD, "reasoning": "First opinion."}
    second = {**GOOD, "reasoning": "Second opinion.", "win_percent": 55}
    ai.replies.extend([anthropic_reply(first), anthropic_reply(second)])
    ask(client, csrf(), cup, match_id)
    clock.current += timedelta(minutes=5)
    ask(client, csrf(), cup, match_id)

    page = client.get(url(cup, match_id)).text

    assert "Second opinion." in page and "First opinion." not in page
    assert len(stored(db)) == 2
