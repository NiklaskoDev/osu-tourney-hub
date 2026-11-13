"""Matches compared from stored scores: rosters, snapshots, pickems and the qualifier seed."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.repos import matches as matches_repo
from app.repos import stages as stages_repo
from app.repos import tournaments as tournaments_repo
from app.services import match_plan, matchup
from app.services.match_plan import PlanError
from tests.matchup_support import (
    CAPTAIN,
    GUEST,
    HD1,
    MATE,
    NM1,
    NM2,
    RIVAL_B,
    SUB,
    TB,
    add_game,
    build_cup,
    fill_scores,
    play,
)
from tests.support import (
    add_match,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_team,
    add_tournament,
)

NOW = datetime(2026, 11, 12, 12, 30, 15, tzinfo=UTC)


@pytest.fixture
def cup(db):
    return build_cup(db)


def view_of(db, cup, match_id):
    tournament = tournaments_repo.get(db, cup["id"])
    return matchup.build_matchup(db, tournament, matches_repo.get(db, match_id))


# --- the comparison ----------------------------------------------------------------------------


def test_the_match_compares_the_expected_scores_map_by_map(db, cup):
    fill_scores(db)
    view = view_of(db, cup, add_game(db, cup))

    rows = {row.pool_map.slot: row for row in view.prediction.comparisons}

    assert rows["NM1"].side1.score == 2_000_000 and rows["NM1"].side2.score == 1_600_000
    assert rows["NM1"].prob > 0.5
    assert rows["NM1"].quality == "actual"
    assert view.left.name == "Alpha Squad" and view.right.name == "Rival Crew"
    assert view.we_play
    assert view.prediction.with_data == 4
    assert view.prediction.series.win_prob > 0.5


def test_our_team_is_on_the_left_even_when_it_is_team_two(db, cup):
    fill_scores(db)
    view = view_of(db, cup, add_game(db, cup, ours_first=False))

    assert (view.left.name, view.right.name) == ("Alpha Squad", "Rival Crew")
    assert view.prediction.series.win_prob > 0.5  # the left side is ours, and we are ahead
    assert view.title == "Alpha Squad vs Rival Crew"


def test_a_match_we_do_not_play_reads_from_team_one(db, cup):
    fill_scores(db)
    other = add_team(db, cup["id"], "Third Team", [(3101, "ThirdA"), (3102, "ThirdB")])
    match_id = add_match(db, cup["id"], cup["rivals"], other, None, stage_id=cup["stage"])

    view = view_of(db, cup, match_id)

    assert not view.we_play
    assert view.left.name == "Rival Crew" and view.right.name == "Third Team"
    assert view.plan == []
    assert view.prediction.with_data == 0  # the third team has no scores


def test_substitutes_and_guests_do_not_count_for_our_side(db, cup):
    fill_scores(db)
    play(db, NM1, SUB, 5_000_000)
    play(db, NM1, GUEST, 5_000_000)

    view = view_of(db, cup, add_game(db, cup))

    nm1 = next(row for row in view.prediction.comparisons if row.pool_map.slot == "NM1")
    assert nm1.side1.score == 2_000_000


def test_a_missing_opponent_score_is_filled_in_and_marked_estimated(db, cup):
    fill_scores(db)
    db.execute("DELETE FROM scores WHERE osu_id = ? AND beatmap_id = ?", (RIVAL_B, NM2))

    view = view_of(db, cup, add_game(db, cup))

    nm2 = next(row for row in view.prediction.comparisons if row.pool_map.slot == "NM2")
    assert nm2.side2.estimated and nm2.quality == "estimated"
    assert nm2.side2.score == pytest.approx(1_600_000)  # their usual share of the team


def test_a_team_without_scores_has_no_data_and_a_coin_flip_series(db, cup):
    for beatmap in (NM1, NM2, HD1, TB):
        play(db, beatmap, CAPTAIN, 900_000)
        play(db, beatmap, MATE, 900_000)

    view = view_of(db, cup, add_game(db, cup))

    assert view.prediction.with_data == 0
    assert view.format_notice == matchup.NO_DATA
    assert {row.quality for row in view.prediction.comparisons} == {"no data"}
    assert view.prediction.series.win_prob == pytest.approx(0.5)


def test_a_stage_without_a_best_of_still_compares_the_maps(db, cup):
    fill_scores(db)
    db.execute("UPDATE stages SET best_of = NULL WHERE id = ?", (cup["stage"],))

    view = view_of(db, cup, add_game(db, cup))

    assert view.prediction.series is None
    assert view.format_notice == matchup.NO_FORMAT
    assert view.prediction.with_data == 4


def test_scores_use_the_tournaments_scoring_type(db, cup):
    fill_scores(db)
    db.execute("UPDATE tournaments SET scoring = 'v1' WHERE id = ?", (cup["id"],))

    view = view_of(db, cup, add_game(db, cup))

    assert view.prediction.with_data == 0  # every score is ScoreV2: none counts for ScoreV1


@pytest.mark.parametrize(
    ("setup", "notice"),
    [
        ("no_team", matchup.NO_TEAMS),
        ("no_stage", matchup.NO_STAGE),
        ("unreleased", matchup.NO_POOL),
    ],
)
def test_a_match_that_cannot_be_compared_says_why(db, cup, setup, notice):
    match_id = add_game(db, cup)
    if setup == "no_team":
        db.execute("UPDATE matches SET team2_id = NULL WHERE id = ?", (match_id,))
    elif setup == "no_stage":
        db.execute("UPDATE matches SET stage_id = NULL WHERE id = ?", (match_id,))
    else:
        db.execute("UPDATE stages SET pool_released = 0 WHERE id = ?", (cup["stage"],))

    view = view_of(db, cup, match_id)

    assert view.notice == notice
    assert view.prediction is None


def test_the_plan_is_loaded_for_matches_we_play(db, cup):
    match_id = add_game(db, cup)
    db.execute(
        "INSERT INTO plan_items (match_id, kind, pool_map_id, position) VALUES (?, 'ban', ?, 1)",
        (match_id, cup["maps"]["NM1"]),
    )

    assert [item.pool_map_id for item in view_of(db, cup, match_id).plan] == [cup["maps"]["NM1"]]


# --- snapshots ---------------------------------------------------------------------------------


def snapshots(db, match_id):
    return db.execute(
        "SELECT * FROM predictions WHERE match_id = ? ORDER BY id", (match_id,)
    ).fetchall()


def test_the_first_view_of_a_day_keeps_a_snapshot_and_later_views_do_not(db, cup):
    fill_scores(db)
    match_id = add_game(db, cup)

    assert matchup.save_daily_snapshot(db, view_of(db, cup, match_id), NOW)
    assert not matchup.save_daily_snapshot(db, view_of(db, cup, match_id), NOW + timedelta(hours=5))
    assert matchup.save_daily_snapshot(db, view_of(db, cup, match_id), NOW + timedelta(days=1))

    rows = snapshots(db, match_id)
    assert [row["created_at"][:10] for row in rows] == ["2026-11-12", "2026-11-13"]
    assert {row["kind"] for row in rows} == {"stat"}
    assert {row["model_version"] for row in rows} == {"stat-2"}


def test_the_snapshot_holds_the_chance_the_score_and_the_prediction_as_json(db, cup):
    fill_scores(db)
    match_id = add_game(db, cup)

    matchup.save_daily_snapshot(db, view_of(db, cup, match_id), NOW)

    row = snapshots(db, match_id)[0]
    payload = json.loads(row["payload"])
    assert row["win_prob_team1"] > 0.5  # team 1 is ours and we are ahead
    assert row["expected_score"] in {"2-0", "2-1"}
    assert payload["side1_team_id"] == cup["ours"] and payload["side2_team_id"] == cup["rivals"]
    assert {m["slot"] for m in payload["maps"]} == {"NM1", "NM2", "HD1", "TB"}
    assert len(payload["orders"]) == 2
    assert payload["plan"] == []


def test_a_snapshot_is_stored_from_team_ones_side_even_when_ours_is_team_two(db, cup):
    fill_scores(db)
    match_id = add_game(db, cup, ours_first=False)

    matchup.save_daily_snapshot(db, view_of(db, cup, match_id), NOW)

    row = snapshots(db, match_id)[0]
    assert row["win_prob_team1"] < 0.5  # team 1 is the rival crew
    wins_team1, wins_team2 = (int(n) for n in row["expected_score"].split("-"))
    assert wins_team2 > wins_team1
    assert json.loads(row["payload"])["side1_team_id"] == cup["ours"]  # the page's left team


def test_a_finished_match_and_a_prediction_without_data_keep_no_snapshot(db, cup):
    fill_scores(db)
    done = add_game(db, cup, status="done", score1=2, score2=1)
    other = add_team(db, cup["id"], "Third Team", [(3101, "ThirdA")])
    blank = add_match(db, cup["id"], cup["ours"], other, None, stage_id=cup["stage"])

    assert not matchup.save_daily_snapshot(db, view_of(db, cup, done), NOW)
    assert not matchup.save_daily_snapshot(db, view_of(db, cup, blank), NOW)
    assert snapshots(db, done) == [] and snapshots(db, blank) == []


# --- the plan ----------------------------------------------------------------------------------


def values(**fields):
    return {name: str(value) for name, value in fields.items()}


def planned(db, match_id):
    rows = db.execute(
        "SELECT kind, pool_map_id, position, note, set_by FROM plan_items WHERE match_id = ? "
        "ORDER BY kind, position",
        (match_id,),
    ).fetchall()
    return [tuple(row) for row in rows]


def save(db, cup, match_id, form, now=NOW):
    tournament = tournaments_repo.get(db, cup["id"])
    return match_plan.save_plan(db, tournament, matches_repo.get(db, match_id), form, CAPTAIN, now)


def test_a_stage_gives_each_team_its_protects_bans_and_its_share_of_the_picks(db, cup):
    stage = stages_repo.get(db, cup["stage"])

    slots = match_plan.plan_slots(stage)

    assert (slots.protects, slots.bans, slots.picks) == (0, 1, 1)  # best of 3: two games
    db.execute(
        "UPDATE stages SET best_of = 9, protects = 2, bans = 2 WHERE id = ?", (cup["stage"],)
    )
    bigger = match_plan.plan_slots(stages_repo.get(db, cup["stage"]))
    assert (bigger.protects, bigger.bans, bigger.picks) == (2, 2, 4)
    db.execute("UPDATE stages SET best_of = NULL WHERE id = ?", (cup["stage"],))
    assert match_plan.plan_slots(stages_repo.get(db, cup["stage"])).picks == 0


def test_saving_a_plan_stores_the_choices_in_order_with_notes(db, cup):
    db.execute("UPDATE stages SET best_of = 7, bans = 2 WHERE id = ?", (cup["stage"],))
    fill_scores(db)
    match_id = add_game(db, cup)
    maps = cup["maps"]

    changed = save(
        db,
        cup,
        match_id,
        {
            "ban_1": "",  # an empty slot is skipped, the next one moves up
            "ban_2": str(maps["HD1"]),
            "ban_note_2": " their best map ",
            "pick_1": str(maps["NM1"]),
            "pick_2": str(maps["NM2"]),
        },
    )

    assert changed
    assert planned(db, match_id) == [
        ("ban", maps["HD1"], 1, "their best map", CAPTAIN),
        ("pick", maps["NM1"], 1, None, CAPTAIN),
        ("pick", maps["NM2"], 2, None, CAPTAIN),
    ]


def test_saving_the_same_plan_again_changes_nothing_and_keeps_no_new_snapshot(db, cup):
    fill_scores(db)
    match_id = add_game(db, cup)
    form = {"ban_1": str(cup["maps"]["HD1"]), "pick_1": str(cup["maps"]["NM1"])}

    assert save(db, cup, match_id, form)
    assert not save(db, cup, match_id, form, NOW + timedelta(minutes=5))

    assert len(snapshots(db, match_id)) == 1


def test_a_changed_plan_saves_a_snapshot_that_holds_the_plan(db, cup):
    fill_scores(db)
    match_id = add_game(db, cup)
    maps = cup["maps"]

    save(db, cup, match_id, {"ban_1": str(maps["HD1"])})
    save(db, cup, match_id, {"ban_1": str(maps["NM2"])}, NOW + timedelta(minutes=5))

    plans = [json.loads(row["payload"])["plan"] for row in snapshots(db, match_id)]
    assert plans == [
        [{"kind": "ban", "pool_map_id": maps["HD1"], "position": 1}],
        [{"kind": "ban", "pool_map_id": maps["NM2"], "position": 1}],
    ]


def test_clearing_the_form_clears_the_plan(db, cup):
    match_id = add_game(db, cup)
    save(db, cup, match_id, {"ban_1": str(cup["maps"]["HD1"])})

    assert save(db, cup, match_id, {"ban_1": ""})
    assert planned(db, match_id) == []


@pytest.mark.parametrize(
    ("form", "field", "message"),
    [
        ({"ban_1": "999999"}, "ban_1", "Choose a map from this stage's pool."),
        ({"ban_1": "abc"}, "ban_1", "Choose a map from this stage's pool."),
        ({"ban_1": "TB"}, "ban_1", "Choose a map from this stage's pool."),
        ({"pick_1": "-3"}, "pick_1", "Choose a map from this stage's pool."),
        ({"ban_note_1": "x" * 301, "ban_1": "NM1"}, "ban_note_1", "at most 300"),
        ({"pick_1": "NM1", "ban_1": "NM1"}, "pick_1", "A banned map cannot be picked."),
    ],
)
def test_a_plan_with_a_bad_choice_is_refused_and_nothing_is_saved(db, cup, form, field, message):
    match_id = add_game(db, cup)
    maps = cup["maps"]
    form = {key: str(maps.get(value, value)) for key, value in form.items()}  # slots to map ids

    with pytest.raises(PlanError, match=message) as refused:
        save(db, cup, match_id, form)

    assert refused.value.field == field and refused.value.status == 422
    assert planned(db, match_id) == []


def test_a_protected_map_cannot_be_banned_and_a_map_is_chosen_once_per_kind(db, cup):
    db.execute(
        "UPDATE stages SET protects = 2, bans = 2, best_of = 7 WHERE id = ?", (cup["stage"],)
    )
    match_id = add_game(db, cup)
    nm1, nm2 = str(cup["maps"]["NM1"]), str(cup["maps"]["NM2"])

    with pytest.raises(PlanError, match="protected map cannot be banned"):
        save(db, cup, match_id, {"protect_1": nm1, "ban_1": nm1})
    with pytest.raises(PlanError, match="twice") as twice:
        save(db, cup, match_id, {"pick_1": nm2, "pick_2": nm2})

    assert twice.value.field == "pick_2"
    assert planned(db, match_id) == []
    # A protected map may still be picked.
    assert save(db, cup, match_id, {"protect_1": nm1, "pick_1": nm1})


def test_slots_beyond_what_the_stage_allows_are_ignored(db, cup):
    match_id = add_game(db, cup)  # best of 3: one ban, no protect, one pick

    save(
        db, cup, match_id, {"protect_1": str(cup["maps"]["NM1"]), "ban_2": str(cup["maps"]["NM2"])}
    )

    assert planned(db, match_id) == []


def test_a_plan_is_only_for_matches_we_play_and_needs_a_stage(db, cup):
    other = add_team(db, cup["id"], "Third Team", [(3101, "ThirdA")])
    foreign = add_match(db, cup["id"], cup["rivals"], other, None, stage_id=cup["stage"])
    stageless = add_match(db, cup["id"], cup["ours"], cup["rivals"], None)

    for match_id in (foreign, stageless):
        with pytest.raises(PlanError) as refused:
            save(db, cup, match_id, {"ban_1": str(cup["maps"]["NM1"])})
        assert refused.value.status == 409


# --- pickems -----------------------------------------------------------------------------------


def test_pickems_predict_every_match_of_the_stage_in_time_order(db, cup):
    fill_scores(db)
    other = add_team(db, cup["id"], "Third Team", [(3101, "ThirdA")])
    later = add_game(db, cup, when="2026-11-15T18:00:00Z")
    first = add_game(db, cup, ours_first=False, when="2026-11-14T18:00:00Z", label="Match 1")
    played = add_match(
        db,
        cup["id"],
        cup["rivals"],
        cup["ours"],
        "2026-11-10T18:00:00Z",
        stage_id=cup["stage"],
        status="done",
        score1=1,
        score2=2,
    )
    undecided = add_match(db, cup["id"], cup["ours"], None, None, stage_id=cup["stage"])
    elsewhere = add_match(db, cup["id"], cup["ours"], other, None)  # a match without a stage
    tournament = tournaments_repo.get(db, cup["id"])
    stage = stages_repo.get(db, cup["stage"])

    view = matchup.build_pickems(db, tournament, stage)

    assert [row.match.id for row in view.rows] == [played, first, later, undecided]
    assert elsewhere not in [row.match.id for row in view.rows]
    by_match = {row.match.id: row for row in view.rows}
    assert by_match[later].winner == "Alpha Squad"
    assert by_match[later].win_chance > 0.5
    assert by_match[later].score in {"2-0", "2-1"}
    assert (by_match[later].with_data, by_match[later].total) == (4, 4)
    assert by_match[first].winner == "Alpha Squad"  # winner and score read from the winner's side
    assert by_match[played].result == "1 - 2"
    assert (
        by_match[undecided].winner is None and by_match[undecided].reason == "Teams not decided yet"
    )
    assert view.copy_text.startswith("Pickems: Round of 16\n")
    assert "Alpha Squad vs Rival Crew: Alpha Squad" in view.copy_text
    assert "Teams not decided" not in view.copy_text


def test_pickems_name_the_reason_when_there_is_no_prediction(db, cup):
    ours_only = add_game(db, cup)
    tournament = tournaments_repo.get(db, cup["id"])
    stage = stages_repo.get(db, cup["stage"])

    row = matchup.build_pickems(db, tournament, stage).rows[0]
    assert row.winner is None and row.reason == "No scores yet for both teams"

    db.execute("UPDATE stages SET best_of = NULL WHERE id = ?", (cup["stage"],))
    fill_scores(db)
    stage = stages_repo.get(db, cup["stage"])
    row = matchup.build_pickems(db, tournament, stage).rows[0]
    assert row.reason == "No best-of set for this stage" and row.match.id == ours_only

    db.execute("UPDATE stages SET pool_released = 0 WHERE id = ?", (cup["stage"],))
    stage = stages_repo.get(db, cup["stage"])
    assert matchup.build_pickems(db, tournament, stage).rows[0].reason == "Pool not published yet"


def test_the_pickem_for_the_underdog_names_the_other_team_with_their_chance(db, cup):
    fill_scores(db, ours=500_000, theirs=900_000)
    add_game(db, cup)
    tournament = tournaments_repo.get(db, cup["id"])
    stage = stages_repo.get(db, cup["stage"])

    row = matchup.build_pickems(db, tournament, stage).rows[0]

    assert row.winner == "Rival Crew"
    assert row.win_chance > 0.5
    one, two = (int(n) for n in row.score.split("-"))
    assert one > two  # the winner's maps come first


# --- the qualifier seed ------------------------------------------------------------------------


@pytest.fixture
def qualifier(db):
    """A qualifier with three maps and teams of one player: ours, three rivals of rising skill
    and one that has played a single map."""
    tid = add_tournament(db, "Open Cup", acronym="OC")
    db.execute("UPDATE tournaments SET is_solo = 1 WHERE id = ?", (tid,))
    stage = add_stage(db, tid, "Qualifiers", lineup_size=1)
    db.execute(
        "UPDATE stages SET is_qualifier = 1, end_at = '2026-11-20T00:00:00Z' WHERE id = ?", (stage,)
    )
    for number, beatmap in enumerate((2001, 2002, 2003), start=1):
        add_pool_map(db, stage, f"NM{number}", beatmap_id=beatmap)
    ours = add_team(db, tid, "Us", [(101, "Us")], ours=True)
    add_member(db, tid, 101, "captain")
    for team, (player, points) in {
        "Slow": (102, 100),
        "Mid": (103, 200),
        "Fast": (104, 300),
    }.items():
        add_team(db, tid, team, [(player, team)])
        for beatmap in (2001, 2002, 2003):
            add_score(db, beatmap, player, points * 1000)
    add_team(db, tid, "Rare", [(105, "Rare")])
    add_score(db, 2001, 105, 900_000)
    for beatmap in (2001, 2002, 2003):
        add_score(db, beatmap, 101, 250_000)
    return {"id": tid, "stage": stage, "ours": ours}


def test_the_seed_line_ranks_us_among_the_teams_with_enough_scores(db, qualifier):
    tournament = tournaments_repo.get(db, qualifier["id"])

    line = matchup.seed_line(db, tournament, NOW)

    assert line.stage_name == "Qualifiers"
    assert line.text == "Estimated seed: 2 of 5 (based on 4 teams with data)"


def test_the_seed_line_says_when_we_have_too_few_scores(db, qualifier):
    db.execute("DELETE FROM scores WHERE osu_id = 101")
    tournament = tournaments_repo.get(db, qualifier["id"])

    line = matchup.seed_line(db, tournament, NOW)

    assert line.text == "Estimated seed: not enough scores yet (3 teams with data)"


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE stages SET is_qualifier = 0",
        "UPDATE stages SET pool_released = 0",
        "UPDATE stages SET end_at = '2026-11-12T12:30:15Z'",  # ends exactly now: over
        "UPDATE tournaments SET our_team_id = NULL",
        "DELETE FROM pool_maps",
    ],
)
def test_there_is_no_seed_line_without_a_running_qualifier_and_a_team_of_ours(
    db, qualifier, change
):
    db.execute(change)
    tournament = tournaments_repo.get(db, qualifier["id"])

    assert matchup.seed_line(db, tournament, NOW) is None
