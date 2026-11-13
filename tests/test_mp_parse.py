"""Games and scores read from lobby events."""

from app.services.mp_parse import last_final_event_id, parse_games


def score(user_id=7, points=800_000, **fields):
    return {
        "user_id": user_id,
        "score": points,
        "accuracy": 0.9812,
        "max_combo": 410,
        "mods": [],
        "statistics": {"count_miss": 3},
        "match": {"slot": 0, "team": "red", "pass": True},
        **fields,
    }


def game(game_id=1, beatmap_id=555, scores=None, **fields):
    return {
        "id": game_id,
        "beatmap_id": beatmap_id,
        "start_time": "2026-11-12T10:10:00+00:00",
        "end_time": "2026-11-12T10:14:00+00:00",
        "mode_int": 0,
        "scoring_type": "scorev2",
        "mods": [],
        "scores": [score()] if scores is None else scores,
        **fields,
    }


def event(event_id, game_value=None):
    return {"id": event_id, **({"game": game_value} if game_value is not None else {})}


def test_a_finished_game_gives_its_scores_in_the_apps_units():
    (parsed,) = parse_games([event(1, game())])

    assert (parsed.game_id, parsed.beatmap_id, parsed.gamemode) == (1, 555, 0)
    assert parsed.played_at == "2026-11-12T10:14:00Z"
    (only,) = parsed.scores
    assert (only.osu_id, only.score, only.accuracy) == (7, 800_000, 98.12)
    assert (only.max_combo, only.misses, only.mods, only.passed) == (410, 3, "", True)


def test_events_without_a_game_are_skipped():
    assert [g.game_id for g in parse_games([event(1), event(2, game(2))])] == [2]
    assert parse_games([event(1), {"id": 2, "detail": {"type": "player-joined"}}]) == []


def test_a_game_that_has_not_ended_is_left_out_until_it_is_over():
    running = game(2, end_time=None, scores=[])

    assert [g.game_id for g in parse_games([event(1, game(1)), event(2, running)])] == [1]


def test_the_scoring_type_maps_to_the_scoring_stored_with_a_score():
    scorings = {
        "scorev2": "v2",
        "score": "v1",
        "accuracy": "unknown",
        "combo": "unknown",
        "something new": "unknown",
    }
    for raw, stored in scorings.items():
        (parsed,) = parse_games([event(1, game(scoring_type=raw))])
        assert parsed.scoring == stored, raw


def test_mods_are_canonical_and_the_lazer_classic_mod_is_ignored():
    mods = [{"acronym": "HD"}, {"acronym": "CL"}, "NF", {"acronym": "DT"}]
    (parsed,) = parse_games([event(1, game(scores=[score(mods=mods)], mods=["HR"]))])

    assert parsed.scores[0].mods == "DTHD"
    assert parsed.mods == "HR"


def test_the_miss_count_reads_the_older_and_the_lazer_statistics():
    scores = [
        score(1, statistics={"count_miss": 4}),
        score(2, statistics={"miss": 6}),
        score(3, statistics={}),
    ]
    (parsed,) = parse_games([event(1, game(scores=scores))])

    assert [s.misses for s in parsed.scores] == [4, 6, None]


def test_pass_comes_from_the_match_info_then_the_score():
    scores = [
        score(1, match={"pass": False}),
        score(2, match=None, passed=False),
        score(3, match=None),
    ]
    (parsed,) = parse_games([event(1, game(scores=scores))])

    assert [s.passed for s in parsed.scores] == [False, False, True]


def test_unusable_scores_are_left_out_and_the_rest_kept():
    bad = [
        score(0),  # no user
        score(5, points=-1),
        score(6, points=2_000_000_001),
        score(8, accuracy=1.5),
        score(9, accuracy="high"),
        score(10, mods=[{"acronym": "ZZ"}]),  # a mod this mode does not have
        "not a score",
    ]
    (parsed,) = parse_games([event(1, game(scores=[*bad, score(7)]))])

    assert [s.osu_id for s in parsed.scores] == [7]


def test_a_game_without_a_usable_id_beatmap_or_mode_is_skipped():
    games = [
        game(0),
        game(beatmap_id=None),
        game(mode_int=None, mode="unknown"),
    ]

    assert parse_games([event(n, g) for n, g in enumerate(games, 1)]) == []


def test_the_mode_falls_back_to_its_name():
    (parsed,) = parse_games([event(1, game(mode_int=None, mode="mania"))])

    assert parsed.gamemode == 3


# --- where a later read may start ---


def test_the_last_final_event_is_the_newest_when_every_game_ended():
    events = [event(10), event(11, game(1)), event(12)]

    assert last_final_event_id(events) == 12


def test_the_last_final_event_stops_before_a_game_in_progress():
    events = [
        event(10),
        event(11, game(1)),
        event(12, game(2, end_time=None, scores=[])),
        event(13),
    ]

    assert last_final_event_id(events) == 11


def test_the_last_final_event_is_none_without_events_or_when_the_first_is_in_progress():
    assert last_final_event_id([]) is None
    assert last_final_event_id([event(5, game(1, end_time=None, scores=[]))]) is None


def test_the_last_final_event_does_not_depend_on_the_order_given():
    events = [event(13), event(12, game(2, end_time=None, scores=[])), event(11, game(1))]

    assert last_final_event_id(events) == 11
