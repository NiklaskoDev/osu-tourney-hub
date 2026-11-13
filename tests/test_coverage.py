"""Practice coverage: the grid, the flags, targets and who may set them."""

from datetime import UTC, datetime

import pytest

from app.domain.models import Score
from app.repos import practice as practice_repo
from app.repos import stages as stages_repo
from app.repos import tournaments as tournaments_repo
from app.repos.practice import Target
from app.services import coverage, pool_sheet
from app.services.coverage import TargetForm, build_coverage, save_target, target_met
from app.services.score_entry import EntryError
from tests.support import (
    ADMIN_ID,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER, SUB, GUEST, STRANGER = 2001, 2002, 2003, 2004, 2005, 2009
NOW = "2026-11-12T12:30:15Z"
DAY = datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC)


@pytest.fixture
def cup(db):
    """Captain, two players, a substitute and a guest; lineup size 2; maps NM1, FM1 and HD1."""
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER, "player")
    add_member(db, tid, SUB, "player", is_substitute=True)
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid, "Qualifiers", lineup_size=2)
    maps = {
        "NM1": add_pool_map(db, stage, "NM1", beatmap_id=1001),
        "FM1": add_pool_map(db, stage, "FM1", beatmap_id=1002, freemod=True),
        "HD1": add_pool_map(db, stage, "HD1", beatmap_id=1003, required_mods="HD"),
    }
    return {"id": tid, "stage": stage, "maps": maps}


def view_of(db, cup):
    tournament = tournaments_repo.get(db, cup["id"])
    data = pool_sheet.load_stage(db, tournament, stages_repo.get(db, cup["stage"]))
    targets = practice_repo.list_for_maps(db, [m.id for m in data.pool])
    return build_coverage(data, tournament, targets)


def row(view, slot):
    return next(r for r in view.rows if r.pool_map.slot == slot)


def cell(view, slot, osu_id):
    columns = [member.osu_id for member in view.columns]
    return row(view, slot).cells[columns.index(osu_id)]


def target(**fields) -> Target:
    values = {
        "id": 1,
        "tournament_id": 1,
        "pool_map_id": 1,
        "osu_id": None,
        "target_score": None,
        "target_acc": None,
        "note": None,
        "set_by": None,
        "updated_at": NOW,
    }
    return Target(**{**values, **fields})


def score(points=800_000, accuracy=97.0) -> Score:
    return Score(
        1, 1001, 1, 0, "", points, "v2", accuracy, None, None, True, None, "manual", "x", None,
        None, None, NOW,
    )  # fmt: skip


# --- is a target met ---


@pytest.mark.parametrize(
    ("fields", "result", "expected"),
    [
        ({"target_score": 800_000}, score(800_000), True),
        ({"target_score": 800_001}, score(800_000), False),
        ({"target_acc": 97.0}, score(accuracy=97.0), True),
        ({"target_acc": 97.01}, score(accuracy=97.0), False),
        ({"target_score": 700_000, "target_acc": 98.0}, score(900_000, 97.5), False),
        ({"target_score": 700_000, "target_acc": 97.0}, score(900_000, 97.5), True),
        ({"target_score": 1}, None, False),
        ({"target_acc": 1.0}, None, False),
    ],
)
def test_a_target_is_met_when_the_score_reaches_every_figure_it_names(fields, result, expected):
    assert target_met(target(**fields), result) is expected


# --- the grid ---


def test_a_cell_shows_the_best_score_and_a_missing_one_is_empty(db, cup):
    add_score(db, 1001, PLAYER, 800_000)
    add_score(db, 1001, PLAYER, 850_000)

    view = view_of(db, cup)

    assert cell(view, "NM1", PLAYER).score.score == 850_000
    assert cell(view, "NM1", OTHER).score is None
    assert row(view, "NM1").with_score == 1
    assert [m.osu_id for m in view.columns] == [CAPTAIN, PLAYER, OTHER, SUB]  # no guest


def test_a_freemod_cell_keeps_the_mods_of_the_best_score(db, cup):
    add_score(db, 1002, PLAYER, 900_000, mods="HD")

    assert cell(view_of(db, cup), "FM1", PLAYER).score.mods == "HD"


def test_the_likely_lineup_is_the_suggestion_without_substitutes(db, cup):
    add_score(db, 1001, CAPTAIN, 900_000)
    add_score(db, 1001, PLAYER, 800_000)
    add_score(db, 1001, OTHER, 700_000)
    add_score(db, 1001, SUB, 990_000)

    view = view_of(db, cup)

    marked = {o for o in (CAPTAIN, PLAYER, OTHER, SUB) if cell(view, "NM1", o).in_likely_lineup}
    assert marked == {CAPTAIN, PLAYER}


def test_a_map_nobody_in_the_likely_lineup_has_a_score_on_is_flagged(db, cup):
    add_score(db, 1001, CAPTAIN, 900_000)
    add_score(db, 1001, PLAYER, 800_000)

    view = view_of(db, cup)

    assert row(view, "FM1").flags == ["Nobody in the likely lineup has a score"]
    assert row(view, "HD1").flags == ["Nobody in the likely lineup has a score"]


def test_a_map_with_fewer_scores_than_the_lineup_size_is_flagged(db, cup):
    add_score(db, 1001, CAPTAIN, 900_000)

    assert row(view_of(db, cup), "NM1").flags == ["Only 1 of 2 have a score"]


def test_a_map_with_enough_scores_has_no_flag(db, cup):
    add_score(db, 1001, CAPTAIN, 900_000)
    add_score(db, 1001, PLAYER, 800_000)

    assert row(view_of(db, cup), "NM1").flags == []


def test_a_substitutes_score_does_not_cover_the_map_for_the_lineup(db, cup):
    add_score(db, 1001, SUB, 900_000)

    view = view_of(db, cup)

    assert row(view, "NM1").flags == ["Nobody in the likely lineup has a score"]
    assert row(view, "NM1").with_score == 1  # but the grid still shows it


def test_a_score_that_does_not_count_for_the_map_is_not_coverage(db, cup):
    add_score(db, 1003, PLAYER, 800_000, mods="")  # HD1 needs HD
    add_score(db, 1001, PLAYER, 800_000, scoring="v1")  # other scoring type

    view = view_of(db, cup)

    assert cell(view, "HD1", PLAYER).score is None
    assert cell(view, "NM1", PLAYER).score is None


# --- targets in the grid ---


def test_a_team_target_applies_to_every_player_and_is_met_or_missed_per_player(db, cup):
    add_score(db, 1001, CAPTAIN, 900_000)
    add_score(db, 1001, PLAYER, 700_000)
    practice_repo.upsert(db, cup["id"], cup["maps"]["NM1"], None, 800_000, None, "x", CAPTAIN, NOW)

    view = view_of(db, cup)

    assert cell(view, "NM1", CAPTAIN).target_met is True
    assert cell(view, "NM1", PLAYER).target_met is False
    assert cell(view, "NM1", OTHER).target_met is False  # no score is a miss
    assert row(view, "NM1").team_target.target_score == 800_000
    assert (
        cell(view, "HD1", CAPTAIN).target is None and cell(view, "HD1", CAPTAIN).target_met is None
    )


def test_a_player_target_replaces_the_team_target_for_that_player(db, cup):
    add_score(db, 1001, PLAYER, 700_000)
    add_score(db, 1001, CAPTAIN, 700_000)
    map_id = cup["maps"]["NM1"]
    practice_repo.upsert(db, cup["id"], map_id, None, 800_000, None, None, CAPTAIN, NOW)
    practice_repo.upsert(db, cup["id"], map_id, PLAYER, 600_000, None, None, CAPTAIN, NOW)

    view = view_of(db, cup)

    assert cell(view, "NM1", PLAYER).target_met is True
    assert cell(view, "NM1", CAPTAIN).target_met is False


# --- saving targets ---


def form(db, cup, **fields):
    values = {
        "pool_map_id": str(cup["maps"]["NM1"]),
        "osu_id": "",
        "target_score": "800000",
        "target_acc": "",
        "note": "",
    }
    return TargetForm(**{**values, **fields})


def save(db, cup, **fields):
    tournament = tournaments_repo.get(db, cup["id"])
    return save_target(db, tournament, form(db, cup, **fields), CAPTAIN, DAY)


def stored(db):
    sql = (
        "SELECT pool_map_id, osu_id, target_score, target_acc, note, set_by FROM practice_targets "
        "ORDER BY id"
    )
    return [tuple(r) for r in db.execute(sql)]


def test_a_team_target_is_saved_with_its_figures_and_note(db, cup):
    assert save(db, cup, target_acc="97,5", note=" Hold the combo ") == "saved"

    assert stored(db) == [(cup["maps"]["NM1"], None, 800_000, 97.5, "Hold the combo", CAPTAIN)]


def test_a_score_is_read_with_separators(db, cup):
    save(db, cup, target_score="1,200,000")

    assert stored(db)[0][2] == 1_200_000


def test_setting_a_target_again_replaces_it_for_the_team_and_for_a_player(db, cup):
    save(db, cup)
    save(db, cup, target_score="900000")
    save(db, cup, osu_id=str(PLAYER), target_score="500000")
    save(db, cup, osu_id=str(PLAYER), target_score="510000")

    assert [(o, s) for _, o, s, *_ in stored(db)] == [(None, 900_000), (PLAYER, 510_000)]


def test_leaving_both_figures_empty_removes_the_target(db, cup):
    save(db, cup)
    save(db, cup, osu_id=str(PLAYER))

    assert save(db, cup, target_score="") == "removed"

    assert [o for _, o, *_ in stored(db)] == [PLAYER]
    assert save(db, cup, target_score="") == "removed"  # nothing there is no error


def test_accuracy_alone_is_a_target(db, cup):
    save(db, cup, target_score="", target_acc="98")

    assert stored(db)[0][2:4] == (None, 98.0)


@pytest.mark.parametrize(
    ("fields", "field_name", "status"),
    [
        ({"pool_map_id": "99999"}, None, 404),
        ({"pool_map_id": "abc"}, None, 404),
        ({"osu_id": str(GUEST)}, "osu_id", 422),
        ({"osu_id": str(STRANGER)}, "osu_id", 422),
        ({"osu_id": "x"}, "osu_id", 422),
        ({"target_score": "lots"}, "target_score", 422),
        ({"target_score": "2000000001"}, "target_score", 422),
        ({"target_score": "-5"}, "target_score", 422),
        ({"target_acc": "100.5"}, "target_acc", 422),
        ({"target_acc": "high"}, "target_acc", 422),
        ({"note": "x" * 501}, "note", 422),
    ],
)
def test_a_refused_form_changes_nothing_and_names_the_field(db, cup, fields, field_name, status):
    with pytest.raises(EntryError) as refused:
        save(db, cup, **fields)

    assert (refused.value.status, refused.value.field) == (status, field_name)
    assert stored(db) == []


def test_a_map_of_another_tournament_is_refused(db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    foreign = add_pool_map(db, add_stage(db, other), "NM1", beatmap_id=1)

    with pytest.raises(EntryError) as refused:
        save(db, cup, pool_map_id=str(foreign))

    assert refused.value.status == 404


def test_targets_leave_with_their_map_or_tournament(db, cup):
    save(db, cup)
    db.execute("DELETE FROM pool_maps WHERE id = ?", (cup["maps"]["NM1"],))
    assert stored(db) == []

    save(db, cup, pool_map_id=str(cup["maps"]["HD1"]))
    db.execute("DELETE FROM tournaments WHERE id = ?", (cup["id"],))
    assert stored(db) == []


def test_the_module_exposes_the_note_limit_the_table_enforces():
    assert coverage.MAX_NOTE_LENGTH == 500


# --- the page ---


def page(client, cup, query=""):
    return client.get(f"/t/{cup['id']}/coverage{query}")


def post(client, cup, csrf, **fields):
    data = {
        **csrf(),
        "stage": str(cup["stage"]),
        "pool_map_id": str(cup["maps"]["NM1"]),
        "osu_id": "",
        "target_score": "800000",
        **fields,
    }
    return client.post(f"/t/{cup['id']}/practice-targets", data=data, follow_redirects=False)


def test_a_stranger_gets_a_404_and_every_member_reads_the_grid(client, login, cup):
    login(STRANGER)
    assert page(client, cup).status_code == 404
    for member in (GUEST, PLAYER, CAPTAIN):
        login(member)
        assert page(client, cup).status_code == 200


def test_the_page_shows_scores_flags_and_the_likely_lineup_in_words(client, login, db, cup):
    add_score(db, 1001, CAPTAIN, 900_000, accuracy=98.5)
    add_score(db, 1002, PLAYER, 850_000, mods="HD")
    login(PLAYER)

    text = page(client, cup).text

    assert "900,000" in text and "98.50%" in text
    assert "Likely lineup" in text
    assert "Only 1 of 2 have a score" in text
    assert "Nobody in the likely lineup has a score" in text
    assert "HD" in text
    assert '1</span> <span class="muted">of 4</span>' in text


def test_the_page_says_met_and_missed_in_words(client, login, db, cup):
    add_score(db, 1001, CAPTAIN, 900_000)
    add_score(db, 1001, PLAYER, 100_000)
    practice_repo.upsert(db, cup["id"], cup["maps"]["NM1"], None, 800_000, None, None, CAPTAIN, NOW)
    login(PLAYER)

    text = page(client, cup).text

    assert text.count("Target met") == 1
    assert text.count("Target missed") == 3  # one below it, two without a score


def test_the_targets_table_lists_who_a_target_is_for(client, login, db, cup):
    practice_repo.upsert(
        db, cup["id"], cup["maps"]["NM1"], None, 800_000, 97.5, "Combo!", CAPTAIN, NOW
    )
    practice_repo.upsert(db, cup["id"], cup["maps"]["HD1"], PLAYER, None, 96.0, None, CAPTAIN, NOW)
    login(PLAYER)

    text = page(client, cup).text

    assert "Whole team" in text and "800,000 and 97.50%" in text and "Combo!" in text
    assert "player" + str(PLAYER) in text and "96.00%" in text


def test_a_stage_without_targets_says_so(client, login, cup):
    login(PLAYER)

    assert "No targets are set for this stage." in page(client, cup).text


def test_a_player_does_not_see_the_form_and_cannot_post(client, login, csrf, db, cup):
    login(PLAYER)
    assert "Save target" not in page(client, cup).text

    response = post(client, cup, csrf)

    assert response.status_code == 403
    assert stored(db) == []


def test_a_captain_sets_a_target_and_sees_it(client, login, csrf, db, cup):
    login(CAPTAIN)
    assert "Save target" in page(client, cup).text

    response = post(client, cup, csrf, osu_id=str(PLAYER), target_acc="97", note="Practice")

    assert response.status_code == 303
    assert response.headers["location"] == f"/t/{cup['id']}/coverage?stage={cup['stage']}"
    assert stored(db) == [(cup["maps"]["NM1"], PLAYER, 800_000, 97.0, "Practice", CAPTAIN)]
    assert "Target saved." in page(client, cup).text


def test_a_captain_removes_a_target_by_clearing_its_figures(client, login, csrf, db, cup):
    login(CAPTAIN)
    post(client, cup, csrf)

    post(client, cup, csrf, target_score="")

    assert stored(db) == []
    assert "Target removed." in page(client, cup).text


def test_a_refused_target_shows_the_error_at_the_field_and_keeps_the_input(
    client, login, csrf, db, cup
):
    login(CAPTAIN)

    response = post(client, cup, csrf, target_score="lots", note="keep me")

    assert response.status_code == 422
    assert "Enter the score as a whole number" in response.text
    assert 'value="lots"' in response.text and 'value="keep me"' in response.text
    assert stored(db) == []


def test_a_map_of_another_tournament_is_a_404_on_the_page(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    foreign = add_pool_map(db, add_stage(db, other), "NM1", beatmap_id=1)
    login(CAPTAIN)

    assert post(client, cup, csrf, pool_map_id=str(foreign)).status_code == 404


def test_an_archived_tournament_takes_no_targets(client, login, csrf, db, cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    assert post(client, cup, csrf).status_code == 409
    assert stored(db) == []


def test_setting_a_target_needs_the_csrf_token(client, login, db, cup):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/practice-targets",
        data={"pool_map_id": str(cup["maps"]["NM1"]), "target_score": "1"},
    )

    assert response.status_code == 403
    assert stored(db) == []


def test_the_stage_tabs_switch_the_grid(client, login, db, cup):
    later = add_stage(db, cup["id"], "Finals", 2)
    add_pool_map(db, later, "NM1", beatmap_id=2001, bracket="NoMod")
    login(PLAYER)

    text = page(client, cup, f"?stage={later}").text

    assert "Finals" in text and "Song NM1" in text
    assert f'href="/t/{cup["id"]}/coverage?stage={later}"' in text
    assert "Song FM1" not in text


def test_a_tournament_without_stages_maps_or_players_says_what_is_missing(client, login, db):
    tid = add_tournament(db, "Empty Cup", acronym="EC")
    login(ADMIN_ID)  # administrators read every tournament
    assert "no stages yet" in client.get(f"/t/{tid}/coverage").text

    stage = add_stage(db, tid, "Quals")
    assert "no maps yet" in client.get(f"/t/{tid}/coverage").text

    add_pool_map(db, stage, "NM1", beatmap_id=5)
    assert "no players on its roster" in client.get(f"/t/{tid}/coverage").text
