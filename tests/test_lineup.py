"""Lineup suggestion: top scorers per map, freemod rules, substitutes, availability."""

from app.domain.models import Member
from app.services import lineup, sheet
from tests.test_sheet import pool_map, score

NM1 = pool_map(1, "NM1", 101)
FM1 = pool_map(2, "FM1", 102, freemod=True)
TB = pool_map(3, "TB", 103, tiebreaker=True)
POOL = [NM1, FM1, TB]


def member(osu_id, role="player", substitute=False):
    return Member(1, osu_id, f"P{osu_id}", None, role, substitute, None, "manual")


def suggestion(scores, players, size=2, rule="none", pool=POOL):
    best = sheet.best_scores(pool, scores, "v2")
    return lineup.suggest(pool, best, players, size, rule)


def picked(result, map_id):
    return [(pick.osu_id, pick.mods, pick.score.score) for pick in result[map_id].picks]


# --- who is eligible ---


def test_guests_never_play_and_substitutes_only_when_asked_for():
    members = [member(1), member(2, "captain"), member(3, substitute=True), member(4, "guest")]

    assert lineup.eligible_players(members) == [1, 2]
    assert lineup.eligible_players(members, include_substitutes=True) == [1, 2, 3]


def test_the_availability_filter_keeps_only_available_players():
    members = [member(1), member(2), member(3)]

    assert lineup.eligible_players(members, available_ids={2, 3, 99}) == [2, 3]
    assert lineup.eligible_players(members, available_ids=set()) == []


# --- top scorers ---


def test_the_top_scorers_of_each_map_are_suggested_best_first():
    scores = [
        score(101, 1, 800_000),
        score(101, 2, 900_000),
        score(101, 3, 700_000),
        score(101, 4, 850_000),
    ]

    result = suggestion(scores, [1, 2, 3, 4], size=2)

    assert picked(result, 1) == [(2, "", 900_000), (4, "", 850_000)]
    assert not result[1].incomplete


def test_a_tie_goes_to_the_higher_accuracy_then_the_more_recent_score():
    scores = [
        score(101, 1, 800_000, accuracy=95.0),
        score(101, 2, 800_000, accuracy=99.0),
        score(101, 3, 800_000, accuracy=95.0, played="2026-11-05T00:00:00Z"),
    ]

    result = suggestion(scores, [1, 2, 3], size=2)

    assert [pick.osu_id for pick in result[1].picks] == [2, 3]


def test_a_map_with_too_few_scorers_is_flagged_incomplete():
    result = suggestion([score(101, 1, 800_000)], [1, 2, 3], size=2)

    assert [pick.osu_id for pick in result[1].picks] == [1]
    assert result[1].incomplete


def test_a_map_nobody_has_played_has_no_picks_and_is_incomplete():
    result = suggestion([], [1, 2], size=1)

    assert all(not s.picks and s.incomplete for s in result.values())


def test_players_outside_the_eligible_list_are_not_suggested():
    scores = [score(101, 1, 900_000), score(101, 2, 800_000)]

    result = suggestion(scores, [2], size=2)

    assert [pick.osu_id for pick in result[1].picks] == [2]


def test_a_tiebreaker_uses_the_players_best_score_whatever_the_mods():
    result = suggestion([score(103, 1, 600_000, mods="HR"), score(103, 2, 500_000)], [1, 2], size=1)

    assert picked(result, 3) == [(1, "HR", 600_000)]


def test_a_mod_map_suggestion_carries_the_required_mods():
    pool = [pool_map(9, "HD1", 109, required="HD")]

    result = suggestion([score(109, 1, 700_000, mods="HD")], [1], size=1, pool=pool)

    assert picked(result, 9) == [(1, "HD", 700_000)]


# --- freemod rules ---


FM_SCORES = [
    score(102, 1, 900_000, mods="HD"),
    score(102, 1, 850_000, mods="HR"),
    score(102, 2, 880_000, mods="HD"),
    score(102, 3, 800_000, mods="HR"),
    score(102, 4, 950_000, mods=""),
]


def test_with_no_freemod_rule_the_best_scores_win_whatever_the_mods():
    result = suggestion(FM_SCORES, [1, 2, 3, 4], size=2, rule="none")

    assert picked(result, 2) == [(4, "", 950_000), (1, "HD", 900_000)]


def test_every_player_must_use_a_mod_leaves_out_nomod_scores():
    result = suggestion(FM_SCORES, [1, 2, 3, 4], size=2, rule="each_player_mod")

    assert picked(result, 2) == [(1, "HD", 900_000), (2, "HD", 880_000)]


def test_every_player_must_use_a_mod_flags_a_map_with_too_few_modded_scores():
    result = suggestion([score(102, 4, 950_000, mods="")], [4], size=1, rule="each_player_mod")

    assert result[2].picks == [] and result[2].incomplete


def test_two_distinct_mods_swaps_the_cheapest_pick_when_everyone_chose_the_same_mod():
    scores = FM_SCORES[:4]  # player 4's nomod score left out

    result = suggestion(scores, [1, 2, 3], size=2, rule="two_distinct_mods")

    # Greedy is HD 900 000 + HD 880 000. The cheapest change gives player 1's HR 850 000 (-50 000);
    # swapping in player 3's HR 800 000 would cost 80 000.
    assert picked(result, 2) == [(1, "HR", 850_000), (2, "HD", 880_000)]
    assert not result[2].rule_unmet


def test_two_distinct_mods_leaves_a_lineup_alone_when_it_already_has_two():
    scores = [score(102, 1, 900_000, mods="HD"), score(102, 2, 800_000, mods="HR")]

    result = suggestion(scores, [1, 2], size=2, rule="two_distinct_mods")

    assert picked(result, 2) == [(1, "HD", 900_000), (2, "HR", 800_000)]
    assert not result[2].rule_unmet


def test_two_distinct_mods_says_so_when_nobody_has_another_mod():
    scores = [score(102, 1, 900_000, mods="HD"), score(102, 2, 800_000, mods="HD")]

    result = suggestion(scores, [1, 2], size=2, rule="two_distinct_mods")

    assert picked(result, 2) == [(1, "HD", 900_000), (2, "HD", 800_000)]
    assert result[2].rule_unmet


def test_a_one_player_lineup_has_nothing_for_a_distinct_mods_rule_to_check():
    result = suggestion(FM_SCORES, [1, 2], size=1, rule="two_distinct_mods")

    assert not result[2].rule_unmet


def test_freemod_rules_only_apply_to_freemod_maps():
    result = suggestion(
        [score(101, 1, 800_000), score(101, 2, 700_000)], [1, 2], 2, "each_player_mod"
    )

    assert len(result[1].picks) == 2


def test_a_score_without_accuracy_ranks_below_a_real_one_in_a_tie():
    scores = [
        score(101, 1, 800_000, accuracy=None),
        score(101, 2, 800_000, accuracy=50.0),
        score(101, 3, 800_000, accuracy=None),
    ]

    result = suggestion(scores, [1, 2, 3], size=2)

    assert [pick.osu_id for pick in result[1].picks][0] == 2  # the real accuracy wins the tie
    assert 2 in [pick.osu_id for pick in result[1].picks]
    assert suggestion(scores[:2], [1, 2], size=1)[1].picks[0].osu_id == 2


def test_a_missing_accuracy_never_beats_a_higher_score():
    scores = [score(101, 1, 900_000, accuracy=None), score(101, 2, 800_000, accuracy=99.0)]

    assert suggestion(scores, [1, 2], size=1)[1].picks[0].osu_id == 1
