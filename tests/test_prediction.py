"""The statistical model: map chances, expected team scores, the simulated series, seeds."""

import itertools
import math

import pytest

from app.domain.models import Stage
from app.services import prediction, sheet
from app.services.prediction import (
    Expectation,
    Habits,
    SeriesMap,
    SeriesResult,
    map_win_prob,
    qualifier_seed,
    simulate_series,
)
from tests.test_sheet import pool_map, score


def stage(best_of=3, bans=None, protects=None) -> Stage:
    return Stage(1, 1, None, "Round of 16", 1, None, None, best_of, bans, protects, False, 2, True)


def series_map(map_id, prob, bracket="NM", tiebreaker=False) -> SeriesMap:
    return SeriesMap(map_id, bracket, prob, tiebreaker)


# --- the chance to win one map -----------------------------------------------------------------


def test_equal_expected_scores_are_a_coin_flip():
    assert map_win_prob(900_000, 900_000) == 0.5


def test_a_lead_of_eight_percent_of_the_mean_wins_about_73_percent():
    assert map_win_prob(104, 96) == pytest.approx(1 / (1 + math.exp(-1)), abs=1e-12)
    assert map_win_prob(104, 96) == pytest.approx(0.73, abs=0.01)


def test_the_chances_of_both_sides_add_up_to_one():
    for ours, theirs in ((900_000, 700_000), (1, 5), (123_456, 123_457)):
        assert map_win_prob(ours, theirs) + map_win_prob(theirs, ours) == pytest.approx(1.0)


def test_a_bigger_lead_never_lowers_the_chance():
    chances = [map_win_prob(score_, 800_000) for score_ in range(400_000, 1_200_001, 100_000)]

    assert chances == sorted(chances)
    assert chances[0] < 0.5 < chances[-1]


def test_no_data_on_either_side_gives_no_chance():
    assert map_win_prob(None, 800_000) is None
    assert map_win_prob(800_000, None) is None
    assert map_win_prob(None, None) is None
    assert map_win_prob(0, 0) is None


# --- expected team score -----------------------------------------------------------------------

M1, M2 = pool_map(1, "NM1", 101), pool_map(2, "NM2", 102)


def best_of_scores(rows):
    """Best scores from (beatmap id, player, points) rows on the pool M1, M2."""
    return sheet.best_scores([M1, M2], [score(b, p, points) for b, p, points in rows], "v2")


def expect(best, players, size, pool_map=M2):
    ratios = prediction.player_ratios([M1, M2], best, players)
    return prediction.expected_team_score(pool_map, best, players, size, ratios)


def test_the_expected_score_sums_the_best_players():
    best = best_of_scores([(102, 1, 900), (102, 2, 800), (102, 3, 700)])

    assert expect(best, [1, 2, 3], 2) == Expectation(1700, estimated=False)


def test_fewer_scorers_than_the_lineup_size_means_no_data():
    best = best_of_scores([(102, 1, 900)])

    assert expect(best, [1, 2], 2) is None
    assert expect(best, [], 1) is None
    assert expect(best, [1], 0) is None


def test_a_missing_player_is_estimated_from_their_usual_share_of_the_team():
    # Player 3 usually scores half the team mean (100 against a mean of 200 on map 1).
    best = best_of_scores(
        [(101, 1, 300), (101, 2, 200), (101, 3, 100), (102, 1, 600), (102, 2, 400)]
    )

    assert expect(best, [1, 2, 3], 3) == Expectation(600 + 400 + 0.5 * 500, estimated=True)


def test_an_estimate_that_is_not_among_the_best_leaves_the_result_actual():
    best = best_of_scores(
        [(101, 1, 300), (101, 2, 200), (101, 3, 100), (102, 1, 600), (102, 2, 400)]
    )

    assert expect(best, [1, 2, 3], 2) == Expectation(1000, estimated=False)


def test_a_player_without_any_score_cannot_be_estimated():
    best = best_of_scores([(102, 1, 600), (102, 2, 400)])

    assert expect(best, [1, 2, 3], 3) is None


def test_player_ratios_are_the_median_share_of_the_team_mean():
    best = best_of_scores(
        [(101, 1, 300), (101, 2, 100), (102, 1, 600), (102, 2, 300), (102, 3, 300)]
    )

    ratios = prediction.player_ratios([M1, M2], best, [1, 2, 3])

    # Map 1: mean 200 -> 1.5 and 0.5. Map 2: mean 400 -> 1.5, 0.75, 0.75.
    assert ratios == {1: 1.5, 2: pytest.approx(0.625), 3: 0.75}


def test_team_expectations_cover_every_pool_map():
    best = best_of_scores([(101, 1, 300)])

    result = prediction.team_expectations([M1, M2], best, [1], 1)

    assert result == {1: Expectation(300, False), 2: None}


# --- the series --------------------------------------------------------------------------------


def total(result: SeriesResult) -> float:
    return sum(result.score_probs.values())


def test_best_of_three_with_a_tiebreaker_by_hand():
    maps = [
        series_map(1, 0.8),
        series_map(2, 0.6),
        series_map(3, 0.3),
        series_map(9, 0.5, "TB", tiebreaker=True),
    ]

    result = simulate_series(maps, best_of=3, bans=0, protects=0)

    # Side 1 picks map 1 (0.8) and side 2 map 3 (side 1 wins that one with 0.3); 1-1 happens with
    # 0.8*0.7 + 0.2*0.3 = 0.62 and the tiebreaker is a coin flip.
    assert result.score_probs == {
        (2, 0): pytest.approx(0.24),
        (0, 2): pytest.approx(0.14),
        (2, 1): pytest.approx(0.31),
        (1, 2): pytest.approx(0.31),
    }
    assert result.win_prob == pytest.approx(0.55)
    assert result.expected_score == (2, 1)
    assert [order.first_pick for order in result.orders] == [1, 2]
    assert result.orders[0].picks == [(1, 1), (2, 3)]
    assert result.orders[1].picks == [(2, 3), (1, 1)]


def test_protects_and_bans_by_hand():
    maps = [
        series_map(1, 0.9),
        series_map(2, 0.7),
        series_map(3, 0.4),
        series_map(4, 0.2),
        series_map(9, 0.5, "TB", tiebreaker=True),
    ]

    result = simulate_series(maps, best_of=3, bans=1, protects=1, first_pick=1)

    first = result.orders[0]
    assert first.protects == {1: [1], 2: [4]}  # each protects the map it is strongest on
    assert first.bans == {1: [3], 2: [2]}  # side 1 bans side 2's best unprotected map, then side 2
    assert first.picks == [(1, 1), (2, 4)]  # only the protected maps are left to pick
    # Games: 0.9 then 0.2, tiebreaker 0.5: 2-0 0.18, 1-1 0.74 -> 2-1 and 1-2 0.37 each, 0-2 0.08.
    assert result.win_prob == pytest.approx(0.18 + 0.37)
    assert total(result) == pytest.approx(1.0)


def test_a_protected_map_cannot_be_banned_and_the_tiebreaker_is_never_picked_or_banned():
    maps = [series_map(1, 0.5), series_map(2, 0.5), series_map(9, 0.5, "TB", tiebreaker=True)]

    result = simulate_series(maps, best_of=3, bans=5, protects=1, first_pick=1)

    order = result.orders[0]
    taken = {m for ids in order.protects.values() for m in ids}
    banned = {m for ids in order.bans.values() for m in ids}
    assert taken == {1, 2} and banned == set()  # both maps protected: nothing is left to ban
    assert 9 not in taken | banned | {m for _, m in order.picks}


def test_the_tiebreaker_decides_a_series_that_reaches_one_win_short():
    maps = [series_map(1, 0.5), series_map(2, 0.5), series_map(9, 0.9, "TB", tiebreaker=True)]

    result = simulate_series(maps, best_of=3, bans=0, protects=0)

    assert result.win_prob == pytest.approx(0.25 + 0.5 * 0.9)


def test_a_best_of_one_is_the_tiebreaker_alone():
    maps = [series_map(1, 0.1), series_map(9, 0.7, "TB", tiebreaker=True)]

    assert simulate_series(maps, 1, 0, 0).win_prob == pytest.approx(0.7)
    assert simulate_series([series_map(1, 0.1)], 1, 0, 0).win_prob == pytest.approx(0.5)


def test_a_pool_that_runs_out_of_picks_plays_the_rest_as_coin_flips():
    result = simulate_series([series_map(1, 0.8)], best_of=3, bans=0, protects=0, first_pick=1)

    assert result.win_prob == pytest.approx(0.4 + 0.5 * 0.5)  # 2-0 0.4, then 1-1 0.5 -> 0.25


def test_a_map_without_data_is_a_coin_flip():
    result = simulate_series([series_map(1, None), series_map(2, None)], 3, 0, 0)

    assert result.win_prob == pytest.approx(0.5)
    assert result.score_probs[(2, 0)] == pytest.approx(0.25)


def test_the_win_chance_of_a_best_of_five_matches_brute_force_over_all_outcomes():
    probs = [0.9, 0.75, 0.6, 0.45, 0.3, 0.2]
    maps = [series_map(i + 1, p) for i, p in enumerate(probs[:5])]
    maps.append(series_map(9, probs[5], "TB", tiebreaker=True))

    result = simulate_series(maps, best_of=5, bans=0, protects=0, first_pick=1)

    prob_of = {m.pool_map_id: m.prob for m in maps}
    played = [prob_of[map_id] for _, map_id in result.orders[0].picks]
    expected = 0.0
    for outcome in itertools.product((True, False), repeat=4):  # the first four games
        chance = math.prod(p if won else 1 - p for won, p in zip(outcome, played, strict=True))
        wins = sum(outcome)
        if wins >= 3:
            expected += chance
        elif wins == 2:
            expected += chance * probs[5]
    assert result.win_prob == pytest.approx(expected)
    assert total(result) == pytest.approx(1.0)


def test_both_first_pick_orders_are_averaged_unless_one_is_given():
    maps = [series_map(i, p) for i, p in ((1, 0.9), (2, 0.7), (3, 0.4), (4, 0.3))]
    maps.append(series_map(9, 0.2, "TB", tiebreaker=True))

    both = simulate_series(maps, 5, 0, 0)
    one = simulate_series(maps, 5, 0, 0, first_pick=1)
    two = simulate_series(maps, 5, 0, 0, first_pick=2)

    assert len(both.orders) == 2 and len(one.orders) == len(two.orders) == 1
    assert both.win_prob == pytest.approx((one.win_prob + two.win_prob) / 2)
    for seat in both.score_probs:
        average = (one.score_probs[seat] + two.score_probs[seat]) / 2
        assert both.score_probs[seat] == pytest.approx(average)


def test_the_first_pick_changes_the_order_but_not_the_set_of_maps():
    maps = [series_map(i, p) for i, p in ((1, 0.9), (2, 0.7), (3, 0.4), (4, 0.3))]

    result = simulate_series(maps, 5, 0, 0)

    one, two = (order.picks for order in result.orders)
    assert one[0] == (1, 1) and two[0] == (2, 4)
    assert {map_id for _, map_id in one} == {map_id for _, map_id in two} == {1, 2, 3, 4}


def test_the_likeliest_score_is_one_the_likelier_winner_wins():
    result = SeriesResult(
        0.52,
        {(3, 0): 0.10, (3, 1): 0.20, (3, 2): 0.22, (0, 3): 0.30, (1, 3): 0.10, (2, 3): 0.08},
        [],
    )

    assert result.expected_score == (3, 2)  # not (0, 3): side 2 is less likely to win


def test_an_underdog_series_reports_the_other_sides_score():
    maps = [series_map(1, 0.2), series_map(2, 0.3), series_map(9, 0.2, "TB", tiebreaker=True)]

    result = simulate_series(maps, best_of=3, bans=0, protects=0)

    assert result.win_prob < 0.5
    assert result.expected_score[0] < result.expected_score[1]


def test_an_even_best_of_is_refused():
    with pytest.raises(ValueError, match="odd"):
        simulate_series([series_map(1, 0.5)], 4, 0, 0)


# --- habits ------------------------------------------------------------------------------------


def even_maps():
    return [
        series_map(1, 0.5, "NM"),
        series_map(2, 0.5, "HD"),
        series_map(3, 0.5, "DT"),
        series_map(9, 0.5, "TB", tiebreaker=True),
    ]


def test_without_history_a_tie_goes_to_the_first_map_of_the_pool():
    result = simulate_series(even_maps(), 3, 0, 0, first_pick=1)

    assert result.orders[0].picks[0] == (1, 1)


def test_a_teams_pick_habit_moves_its_pick_to_the_bracket_it_favours():
    habits = (Habits({"HD": 0.8}, {}), None)

    result = simulate_series(even_maps(), 3, 0, 0, first_pick=1, habits=habits)

    assert result.orders[0].picks[0] == (1, 2)


def test_a_ban_habit_moves_the_ban_to_the_bracket_the_team_bans():
    habits = (None, Habits({}, {"DT": 0.9}))

    result = simulate_series(even_maps(), 3, 1, 0, first_pick=2, habits=habits)

    assert result.orders[0].bans[2] == [3]


def test_habits_only_bend_choices_they_do_not_override_a_clear_advantage():
    maps = [series_map(1, 0.8, "NM"), series_map(2, 0.5, "HD"), series_map(9, 0.5, "TB", True)]
    habits = (Habits({"HD": 0.5}, {}), None)  # a shift of 0.15 against an advantage gap of 0.3

    result = simulate_series(maps, 3, 0, 0, first_pick=1, habits=habits)

    assert result.orders[0].picks[0] == (1, 1)


# --- the format of a stage ---------------------------------------------------------------------


def test_the_series_format_comes_from_the_stage():
    assert prediction.series_format(stage(9, 2, 1)) == prediction.SeriesFormat(9, 2, 1)
    assert prediction.series_format(stage(7, None, None)) == prediction.SeriesFormat(7, 0, 0)


@pytest.mark.parametrize("best_of", [None, 0, 4, 8])
def test_a_stage_without_a_clear_best_of_has_no_format(best_of):
    assert prediction.series_format(stage(best_of)) is None


# --- maps of a match ---------------------------------------------------------------------------


def test_the_comparison_names_the_quality_of_the_data():
    solid, guessed = Expectation(900, False), Expectation(800, True)
    pool = [pool_map(1, "NM1", 101), pool_map(2, "NM2", 102), pool_map(3, "NM3", 103)]

    rows = prediction.compare_maps(
        pool, {1: solid, 2: solid, 3: None}, {1: solid, 2: guessed, 3: solid}
    )

    assert [row.quality for row in rows] == ["actual", "estimated", "no data"]
    assert rows[0].prob == 0.5 and rows[2].prob is None


def test_a_match_prediction_counts_the_maps_with_data_and_needs_a_format_for_the_series():
    pool = [pool_map(1, "NM1", 101), pool_map(2, "NM2", 102)]
    one = {1: Expectation(900, False), 2: None}
    two = {1: Expectation(800, False), 2: Expectation(700, False)}

    with_format = prediction.predict_match(pool, stage(3), one, two)
    without = prediction.predict_match(pool, stage(None), one, two)

    assert (with_format.with_data, with_format.total) == (1, 2)
    assert with_format.series is not None and with_format.series.win_prob > 0.5
    assert without.series is None and without.with_data == 1


# --- qualifier seed ----------------------------------------------------------------------------


def expectations(**per_team):
    return {
        int(team[1:]): {m: Expectation(s, False) for m, s in maps.items()}
        for team, maps in per_team.items()
    }


def test_the_seed_ranks_teams_by_score_relative_to_each_maps_median():
    teams = expectations(
        t1={10: 100, 20: 200, 30: 10},
        t2={10: 200, 20: 400, 30: 20},
        t3={10: 300, 20: 600, 30: 30},
        t4={10: 50},
    )

    mid = qualifier_seed(2, teams, [10, 20, 30])
    top = qualifier_seed(3, teams, [10, 20, 30])
    out = qualifier_seed(4, teams, [10, 20, 30])

    assert (mid.seed, mid.ranked, mid.left_out) == (2, 3, 1)
    assert top.seed == 1
    assert out.seed is None and (out.ranked, out.left_out) == (3, 1)


def test_a_hard_map_does_not_count_more_than_an_easy_one():
    # Raw points on map 10 are a thousand times those on map 20; relative to each map's median
    # (100 000 and 100) team 1 and team 2 are each far ahead on one map and behind on the other.
    teams = expectations(
        t1={10: 200_000, 20: 50},
        t2={10: 50_000, 20: 200},
        t3={10: 100_000, 20: 100},
    )

    first, second, third = (qualifier_seed(team, teams, [10, 20]) for team in (1, 2, 3))

    assert first.seed == second.seed == 1
    assert third.seed == 3


def test_a_team_needs_scores_on_half_of_the_maps_to_be_ranked():
    teams = expectations(t1={1: 10, 2: 10, 3: 10, 4: 10}, t2={1: 10, 2: 10}, t3={1: 10})

    result = qualifier_seed(1, teams, [1, 2, 3, 4])

    assert (result.ranked, result.left_out) == (2, 1)  # exactly half is enough, a quarter is not


def test_teams_with_equal_totals_share_a_seed():
    teams = expectations(t1={1: 100}, t2={1: 100}, t3={1: 50})

    assert qualifier_seed(1, teams, [1]).seed == qualifier_seed(2, teams, [1]).seed == 1
    assert qualifier_seed(3, teams, [1]).seed == 3


def test_there_is_no_seed_without_maps_or_teams_of_ours():
    teams = expectations(t1={1: 100})

    assert qualifier_seed(1, teams, []).seed is None
    assert qualifier_seed(7, teams, [1]).seed is None
    assert qualifier_seed(1, {}, [1]) == prediction.SeedEstimate(None, 0, 0)
