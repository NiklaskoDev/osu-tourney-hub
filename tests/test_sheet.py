"""Best score per player and map, averages, and the rules for which scores count."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.domain.models import PoolMap, Score
from app.services import sheet
from app.services.sheet import OLD_SCORE_DAYS

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)


def pool_map(map_id, slot, beatmap_id, required="", freemod=False, tiebreaker=False, bracket=None):
    return PoolMap(
        id=map_id,
        stage_id=1,
        slot=slot,
        bracket=bracket or slot.rstrip("0123456789"),
        required_mods=required,
        is_freemod=freemod,
        is_tiebreaker=tiebreaker,
        sort_order=map_id,
        beatmap_id=beatmap_id,
        beatmapset_id=None,
        artist=None,
        title=None,
        version=None,
        mapper=None,
        star_rating=None,
        bpm=None,
        length_s=None,
        cs=None,
        ar=None,
        od=None,
        hp=None,
    )


_ids = iter(range(1, 10_000))


def score(
    beatmap_id,
    osu_id,
    points,
    mods="",
    accuracy=97.0,
    scoring="v2",
    played="2026-11-01T12:00:00Z",
    passed=True,
):
    return Score(
        id=next(_ids),
        beatmap_id=beatmap_id,
        osu_id=osu_id,
        gamemode=0,
        mods=mods,
        score=points,
        scoring=scoring,
        accuracy=accuracy,
        max_combo=None,
        misses=None,
        passed=passed,
        achieved_at=played,
        source="manual",
        source_ref="x",
        source_url=None,
        entered_by=None,
        tournament_id=None,
        created_at=played,
    )


NM1 = pool_map(1, "NM1", 101)
NM2 = pool_map(2, "NM2", 102)
HD1 = pool_map(3, "HD1", 103, required="HD")
DT1 = pool_map(4, "DT1", 104, required="DT")
FM1 = pool_map(5, "FM1", 105, freemod=True)
TB = pool_map(6, "TB", 106, tiebreaker=True)
POOL = [NM1, NM2, HD1, DT1, FM1, TB]


def best_of(scores, scoring="v2", pool=POOL):
    return sheet.best_scores(pool, scores, scoring)


# --- best score per player and map ---


def test_the_higher_score_wins():
    best = best_of([score(101, 1, 800_000), score(101, 1, 900_000), score(101, 1, 850_000)])

    assert best[(1, 1)].overall.score == 900_000


def test_players_and_maps_are_kept_apart():
    best = best_of([score(101, 1, 800_000), score(101, 2, 700_000), score(102, 1, 600_000)])

    assert {key: value.overall.score for key, value in best.items()} == {
        (1, 1): 800_000,
        (2, 1): 700_000,
        (1, 2): 600_000,
    }


def test_a_tie_goes_to_the_higher_accuracy_then_to_the_more_recent_score():
    higher_accuracy = best_of(
        [score(101, 1, 800_000, accuracy=96.0), score(101, 1, 800_000, accuracy=98.5)]
    )
    more_recent = best_of(
        [
            score(101, 1, 800_000, played="2026-10-01T00:00:00Z"),
            score(101, 1, 800_000, played="2026-11-01T00:00:00Z"),
        ]
    )

    assert higher_accuracy[(1, 1)].overall.accuracy == 98.5
    assert more_recent[(1, 1)].overall.played_at == "2026-11-01T00:00:00Z"


def test_a_nomod_map_rejects_scores_with_mods():
    best = best_of([score(101, 1, 900_000, mods="HD"), score(101, 1, 800_000)])

    assert best[(1, 1)].overall.score == 800_000
    assert (1, 2) not in best_of([score(102, 1, 900_000, mods="HR")])


def test_no_fail_is_ignored_because_stored_mods_never_carry_it():
    # canonical_mods drops NF before a score is stored, so an NF play is a plain NM play.
    assert (1, 1) in best_of([score(101, 1, 800_000, mods="")])


def test_a_mod_map_accepts_only_its_required_mods():
    best = best_of(
        [
            score(103, 1, 900_000, mods=""),
            score(103, 1, 850_000, mods="HDHR"),
            score(103, 1, 800_000, mods="HD"),
        ]
    )

    assert best[(1, 3)].overall.mods == "HD" and best[(1, 3)].overall.score == 800_000


def test_nightcore_counts_on_a_double_time_map():
    best = best_of([score(104, 1, 800_000, mods="NC"), score(104, 2, 700_000, mods="HD")])

    assert (1, 4) in best and (2, 4) not in best


def test_a_freemod_map_keeps_the_best_per_mod_set_and_shows_the_overall_best():
    best = best_of(
        [
            score(105, 1, 700_000, mods=""),
            score(105, 1, 900_000, mods="HD"),
            score(105, 1, 850_000, mods="HR"),
            score(105, 1, 880_000, mods="HD"),
        ]
    )

    found = best[(1, 5)]
    assert found.overall.score == 900_000 and found.overall.mods == "HD"
    assert {mods: s.score for mods, s in found.by_mods.items()} == {
        "": 700_000,
        "HD": 900_000,
        "HR": 850_000,
    }


def test_a_tiebreaker_accepts_any_mods():
    best = best_of([score(106, 1, 600_000, mods="HDHR")])

    assert best[(1, 6)].overall.mods == "HDHR"


def test_a_score_on_a_beatmap_outside_the_pool_is_ignored():
    assert best_of([score(999, 1, 900_000)]) == {}


def test_a_placeholder_map_without_a_beatmap_never_gets_scores():
    placeholder = pool_map(7, "NM3", None)

    assert best_of([score(101, 1, 900_000)], pool=[placeholder]) == {}


def test_failed_scores_do_not_count():
    assert best_of([score(101, 1, 900_000, passed=False)]) == {}


def test_only_scores_in_the_tournaments_scoring_type_count():
    scores = [
        score(101, 1, 900_000, scoring="v2"),
        score(101, 1, 950_000, scoring="v1"),
        score(101, 1, 960_000, scoring="unknown"),
        score(101, 1, 990_000, scoring="lazer"),
    ]

    assert best_of(scores, "lazer")[(1, 1)].overall.score == 990_000
    assert best_of(scores, "v1")[(1, 1)].overall.score == 950_000
    assert best_of(scores[:3], "v2")[(1, 1)].overall.score == 900_000  # v1 never mixes into v2
    assert best_of([scores[1], scores[2]], "v2") == {}


def test_a_lazer_score_counts_on_a_v2_tournament_as_a_stand_in():
    lazer = score(101, 1, 950_000, scoring="lazer")

    assert sheet.counts_for(lazer, "v2")
    assert sheet.is_stand_in(lazer, "v2")
    assert sheet.uncounted_reason(NM1, lazer, "v2") is None
    assert best_of([lazer])[(1, 1)].overall.score == 950_000


def test_a_lazer_score_still_needs_to_be_passed_and_to_match_the_mods_on_v2():
    assert not sheet.counts_for(score(101, 1, 1, scoring="lazer", passed=False), "v2")
    assert sheet.uncounted_reason(NM1, score(101, 1, 1, scoring="lazer", mods="HD"), "v2") == (
        "Mods do not match the map"
    )


def test_a_v1_score_does_not_count_on_a_v2_tournament():
    v1 = score(101, 1, 950_000, scoring="v1")

    assert not sheet.counts_for(v1, "v2")
    assert not sheet.is_stand_in(v1, "v2")
    assert sheet.uncounted_reason(NM1, v1, "v2") == "Other scoring type"


def test_a_v2_score_does_not_count_on_a_lazer_tournament():
    v2 = score(101, 1, 950_000, scoring="v2")

    assert not sheet.counts_for(v2, "lazer")
    assert not sheet.is_stand_in(v2, "lazer")
    assert sheet.uncounted_reason(NM1, v2, "lazer") == "Other scoring type"


def test_a_lazer_score_does_not_count_on_a_v1_tournament():
    lazer = score(101, 1, 950_000, scoring="lazer")

    assert not sheet.counts_for(lazer, "v1")
    assert not sheet.is_stand_in(lazer, "v1")
    assert sheet.uncounted_reason(NM1, lazer, "v1") == "Other scoring type"


def test_a_real_v2_score_is_not_a_stand_in_and_lazer_on_lazer_is_not_either():
    assert not sheet.is_stand_in(score(101, 1, 1, scoring="v2"), "v2")
    assert not sheet.is_stand_in(score(101, 1, 1, scoring="lazer"), "lazer")


def test_the_better_of_a_lazer_and_a_v2_score_wins_on_a_v2_tournament():
    scores = [
        score(101, 1, 940_000, scoring="v2"),
        score(101, 1, 950_000, scoring="lazer"),
    ]

    best = best_of(scores, "v2")[(1, 1)].overall

    assert best.score == 950_000 and best.scoring == "lazer"


def test_old_scores_still_count_but_are_marked():
    old = score(101, 1, 900_000, played="2025-01-01T00:00:00Z")

    assert best_of([old])[(1, 1)].overall.score == 900_000
    assert sheet.is_old(old, NOW)


@pytest.mark.parametrize(
    ("played", "expected"),
    [
        ("2026-05-14T12:00:00Z", False),  # exactly 182 days before NOW
        ("2026-05-14T11:59:59Z", True),
        ("2026-11-12T11:00:00Z", False),
    ],
)
def test_old_means_more_than_182_days(played, expected):
    assert OLD_SCORE_DAYS == 182
    assert sheet.is_old(score(101, 1, 1, played=played), NOW) is expected


def test_a_score_without_a_play_date_ages_from_when_it_was_added():
    added = score(101, 1, 1, played="2026-11-01T00:00:00Z")
    added = replace(added, achieved_at=None)

    assert added.played_at == "2026-11-01T00:00:00Z"
    assert not sheet.is_old(added, NOW)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"passed": False}, "Not passed"),
        ({"scoring": "v1"}, "Other scoring type"),
        ({"scoring": "lazer"}, None),  # a stand-in on a ScoreV2 tournament
        ({"mods": "HD"}, "Mods do not match the map"),
        ({}, None),
    ],
)
def test_the_reason_a_score_does_not_count(kwargs, reason):
    assert sheet.uncounted_reason(NM1, score(101, 1, 1, **kwargs), "v2") == reason


# --- averages ---


def test_a_personal_average_ignores_maps_without_a_score_and_reports_coverage():
    best = best_of(
        [
            score(101, 1, 800_000, accuracy=96.0),
            score(102, 1, 900_000, accuracy=98.0),
            score(103, 1, 700_000, mods="HD", accuracy=94.0),
        ]
    )

    average = sheet.personal_average(POOL, best, 1)

    assert average.mean_score == pytest.approx(800_000)
    assert average.mean_accuracy == pytest.approx(96.0)
    assert (average.covered, average.total) == (3, 6)


def test_an_average_per_bracket_only_looks_at_that_bracket():
    best = best_of([score(101, 1, 800_000), score(102, 1, 900_000), score(103, 1, 100, mods="HD")])

    nomod = sheet.personal_average([NM1, NM2], best, 1)
    hidden = sheet.personal_average([HD1], best, 1)

    assert (nomod.mean_score, nomod.covered, nomod.total) == (850_000, 2, 2)
    assert (hidden.mean_score, hidden.covered, hidden.total) == (100, 1, 1)


def test_a_player_without_scores_has_no_average_but_a_total():
    average = sheet.personal_average(POOL, {}, 1)

    assert average == sheet.Average(None, None, 0, 6)


def test_a_map_average_counts_only_players_with_a_score():
    best = best_of([score(101, 1, 800_000, accuracy=96), score(101, 2, 600_000, accuracy=94)])

    assert sheet.map_average(NM1, best, [1, 2, 3]) == (700_000, 95.0, 2)
    assert sheet.map_average(NM2, best, [1, 2, 3]) == (None, None, 0)


def test_the_team_average_is_the_mean_of_the_map_means_over_played_maps():
    best = best_of(
        [
            score(101, 1, 800_000),
            score(101, 2, 600_000),  # map mean 700 000
            score(102, 1, 900_000),  # map mean 900 000
        ]
    )

    average = sheet.team_average(POOL, best, [1, 2, 3])

    assert average.mean_score == pytest.approx(800_000)
    assert (average.covered, average.total) == (2, 6)


def test_a_team_without_scores_has_no_average():
    assert sheet.team_average(POOL, {}, [1, 2]).mean_score is None


# --- scores without an accuracy ---


def test_a_score_without_accuracy_loses_a_tie_to_one_with_accuracy():
    plain = score(101, 1, 800_000, accuracy=None)
    exact = score(101, 2, 800_000, accuracy=60.0)

    assert sheet.rank_key(plain) < sheet.rank_key(exact)
    assert best_of([plain, exact])[(1, 1)].overall is plain  # alone it still counts


def test_the_average_accuracy_skips_scores_that_have_none():
    scores = [score(101, 1, 800_000, accuracy=96.0), score(101, 2, 700_000, accuracy=None)]

    team_score, team_accuracy, count = sheet.map_average(NM1, best_of(scores), [1, 2])

    assert (team_score, team_accuracy, count) == (750_000, 96.0, 2)


def test_an_average_accuracy_is_empty_when_no_score_has_one():
    best = best_of([score(101, 1, 800_000, accuracy=None)])

    assert sheet.map_average(NM1, best, [1]) == (800_000, None, 1)
    assert sheet.personal_average(POOL, best, 1).mean_accuracy is None
    assert sheet.team_average(POOL, best, [1]).mean_accuracy is None
