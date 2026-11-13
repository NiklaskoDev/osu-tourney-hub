"""Match history: events from wybin bans and lobby games, a captain's corrections, the review of a
prediction against what was played, and the habits that feed the simulation."""

import asyncio
import json

import pytest

from app.clients.http import make_http_client
from app.clients.osu import OsuClient, OsuNotFound, OsuUnavailable
from app.clients.ratelimit import Priority
from app.clients.wybin import WybinClient
from app.domain.models import MpGame, MpScore, PoolMap
from app.repos import app_settings as app_settings_repo
from app.repos import lobbies as lobbies_repo
from app.repos import match_events as events_repo
from app.repos import matches as matches_repo
from app.repos import predictions as predictions_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo
from app.repos.match_events import MatchEvent
from app.services import history, matchup, prediction
from app.services.history import PICK_SEQ_START, HistoryError
from app.services.wybin_sync import import_tournament, sync_tournament
from tests.matchup_support import (
    CAPTAIN,
    HD1,
    MATE,
    NM1,
    NM2,
    RIVAL_A,
    RIVAL_B,
    TB,
    add_game,
    build_cup,
    fill_scores,
)
from tests.support import ADMIN_ID, add_user

NOW_ISO = "2026-11-14T12:00:00Z"


def run(coroutine):
    return asyncio.run(coroutine)


# --- helpers for the pure functions ------------------------------------------------------------


def pool_map(map_id, slot, beatmap_id, bracket="NM", tiebreaker=False):
    return PoolMap(
        map_id, 1, slot, bracket, "", False, tiebreaker, map_id, beatmap_id,
        None, None, None, None, None, None, None, None, None, None, None, None,
    )  # fmt: skip


POOL = [
    pool_map(1, "NM1", 11),
    pool_map(2, "NM2", 12),
    pool_map(3, "HD1", 13, bracket="HD"),
    pool_map(4, "TB", 14, bracket="TB", tiebreaker=True),
]


def score(osu_id, points, team=None):
    return MpScore(osu_id, points, 98.0, 400, 1, "", True, team)


def game(game_id, beatmap_id, *scores):
    return MpGame(game_id, beatmap_id, 0, "v2", "", "2026-11-14T18:10:00Z", list(scores))


SIDES = {10: 1, 11: 1, 20: 2, 21: 2}  # osu id -> side


def event(seq, kind="pick", side=None, map_id=None, winner=None, source="mp", beatmap=None):
    return MatchEvent(seq, kind, side, map_id, beatmap, winner, None, None, source)


# --- events from the lobby ---------------------------------------------------------------------


def test_every_played_map_becomes_a_pick_in_play_order_with_its_winner():
    games = [
        game(1, 12, score(10, 900), score(11, 800), score(20, 700), score(21, 600)),
        game(2, 11, score(10, 500), score(11, 500), score(20, 600), score(21, 700)),
    ]

    found = history.derive_mp_events(games, POOL, SIDES)

    assert [(e.seq, e.kind, e.pool_map_id, e.winner_side) for e in found] == [
        (PICK_SEQ_START, "pick", 2, 1),
        (PICK_SEQ_START + 1, "pick", 1, 2),
    ]
    assert (found[0].team1_score, found[0].team2_score) == (1700, 1300)
    assert {e.source for e in found} == {"mp"}
    assert {e.team_side for e in found} == {None}  # the lobby does not say who picked


def test_the_tiebreaker_is_not_a_teams_pick():
    found = history.derive_mp_events([game(1, 14, score(10, 5), score(20, 4))], POOL, SIDES)

    assert [(e.kind, e.team_side, e.winner_side) for e in found] == [("tiebreaker", None, 1)]


def test_an_aborted_game_without_scores_is_not_a_pick():
    games = [game(1, 11), game(2, 12, score(10, 5), score(20, 4))]

    found = history.derive_mp_events(games, POOL, SIDES)

    assert [e.pool_map_id for e in found] == [2]


def test_a_map_played_again_counts_once_with_the_last_result_at_its_first_place():
    games = [
        game(1, 11, score(10, 100), score(20, 900)),  # team 2 won the first try
        game(2, 12, score(10, 900), score(20, 100)),
        game(3, 11, score(10, 900), score(20, 100)),  # replayed after the restart
    ]

    found = history.derive_mp_events(games, POOL, SIDES)

    assert [(e.seq, e.pool_map_id, e.winner_side) for e in found] == [
        (PICK_SEQ_START, 1, 1),
        (PICK_SEQ_START + 1, 2, 1),
    ]


def test_warm_ups_outside_the_pool_are_skipped_but_kept_when_no_pool_is_known():
    games = [game(1, 999, score(10, 5), score(20, 4)), game(2, 11, score(10, 5), score(20, 4))]

    with_pool = history.derive_mp_events(games, POOL, SIDES)
    without_pool = history.derive_mp_events(games, [], SIDES)

    assert [e.pool_map_id for e in with_pool] == [1]
    assert [(e.pool_map_id, e.beatmap_id) for e in without_pool] == [(None, 999), (None, 11)]


def test_a_players_side_comes_from_the_roster_not_from_the_colour():
    # Both colours are swapped against what the rosters say for the unknown player 99.
    games = [
        game(
            1, 11,
            score(10, 100, "red"), score(11, 100, "red"), score(20, 100, "blue"),
            score(99, 500, "blue"),
        )
    ]  # fmt: skip

    found = history.derive_mp_events(games, POOL, SIDES)

    assert (found[0].team1_score, found[0].team2_score) == (200, 600)  # 99 joined blue = side 2
    assert found[0].winner_side == 2


def test_a_stand_in_without_a_colour_counts_for_nobody():
    games = [game(1, 11, score(10, 100), score(20, 300), score(99, 900))]

    found = history.derive_mp_events(games, POOL, SIDES)

    assert (found[0].team1_score, found[0].team2_score) == (100, 300)


def test_colours_on_one_side_only_are_not_used_to_place_strangers():
    # Both rostered players wear red, so "red" says nothing about a stand-in.
    games = [game(1, 11, score(10, 100, "red"), score(20, 100, "red"), score(99, 900, "red"))]

    found = history.derive_mp_events(games, POOL, SIDES)

    assert (found[0].team1_score, found[0].team2_score) == (100, 100)
    assert found[0].winner_side is None


def test_a_tied_game_has_no_winner_and_unplaceable_scores_leave_the_result_empty():
    tied = history.derive_mp_events([game(1, 11, score(10, 5), score(20, 5))], POOL, SIDES)
    strangers = history.derive_mp_events([game(1, 11, score(98, 5), score(99, 9))], POOL, SIDES)

    assert tied[0].winner_side is None
    assert (strangers[0].team1_score, strangers[0].team2_score, strangers[0].winner_side) == (
        None,
        None,
        None,
    )


# --- events from wybin -------------------------------------------------------------------------


def test_wybin_bans_alternate_between_the_sides_and_skip_maps_outside_the_pool():
    found = history.wybin_events(POOL, (11, 13), (12, 999))

    assert [(e.seq, e.kind, e.team_side, e.pool_map_id, e.source) for e in found] == [
        (1, "ban", 1, 1, "wybin"),
        (2, "ban", 2, 2, "wybin"),
        (3, "ban", 1, 3, "wybin"),
    ]


def test_uneven_ban_lists_are_read_to_the_end():
    found = history.wybin_events(POOL, (), (12, 13))

    assert [(e.team_side, e.pool_map_id) for e in found] == [(2, 2), (2, 3)]


# --- picks alternate from an anchor ------------------------------------------------------------


def test_without_an_anchor_no_pick_gets_a_side():
    events = [event(100), event(101)]

    assert history.assign_pick_sides(events) == events


def test_picks_alternate_from_the_first_pick_a_captain_named():
    events = [
        event(100),
        event(101, side=2, source="manual"),
        event(102),
        event(103, kind="tiebreaker"),
        event(104),
    ]

    sides = [e.team_side for e in history.assign_pick_sides(events)]

    # the anchor is pick 2 (side 2); pick 1 before it was the other side, the tiebreaker has none
    assert sides == [1, 2, 1, None, 2]


def test_a_manual_pick_keeps_its_own_side_while_the_others_follow_the_first_anchor():
    events = [
        event(100, side=1, source="manual"),
        event(101),
        event(102, side=1, source="manual"),
        event(103),
    ]

    sides = [e.team_side for e in history.assign_pick_sides(events)]

    assert sides == [1, 2, 1, 2]


def test_without_a_manual_side_the_derived_picks_lose_theirs():
    events = [event(100, side=1), event(101, side=2), event(102, "ban", 1, source="wybin")]

    found = history.assign_pick_sides(events)

    assert [e.team_side for e in found] == [None, None, 1]


# --- storing and correcting --------------------------------------------------------------------


@pytest.fixture
def cup(db):
    return build_cup(db)


@pytest.fixture
def match(db, cup):
    match_id = add_game(db, cup, status="done", score1=2, score2=1)
    return matches_repo.get(db, match_id)


def stored(db, match_id, source=None):
    return [
        (e.seq, e.kind, e.team_side, e.pool_map_id, e.winner_side, e.source)
        for e in events_repo.list_for_match(db, match_id)
        if source is None or e.source == source
    ]


def lobby(osu, *picks, lobby_id=777):
    """A lobby in which the captain and a rival played the given beatmaps; each pick is
    (beatmap id, our score, their score)."""
    osu.add_match(lobby_id)
    for beatmap, ours, theirs in picks:
        osu.add_game(lobby_id, beatmap, [(CAPTAIN, ours), (RIVAL_A, theirs)])


def point_at_lobby(db, match, lobby_id=777):
    db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (lobby_id, match.id))
    return matches_repo.get(db, match.id)


def test_reading_a_lobby_stores_the_games_as_picks(db, cup, match, osu, scan_client):
    lobby(osu, (NM1, 900, 800), (HD1, 700, 800), (TB, 900, 800))
    linked = point_at_lobby(db, match)

    count = run(history.refresh_from_lobby(db, scan_client, linked))

    maps = cup["maps"]
    assert count == 3
    assert stored(db, match.id) == [
        (100, "pick", None, maps["NM1"], 1, "mp"),
        (101, "pick", None, maps["HD1"], 2, "mp"),
        (102, "tiebreaker", None, maps["TB"], 1, "mp"),
    ]


def test_reading_again_replaces_the_lobby_events_instead_of_adding_to_them(
    db, cup, match, osu, scan_client
):
    lobby(osu, (NM1, 900, 800))
    linked = point_at_lobby(db, match)
    run(history.refresh_from_lobby(db, scan_client, linked))

    osu.add_game(777, NM2, [(CAPTAIN, 1), (RIVAL_A, 2)])
    run(history.refresh_from_lobby(db, scan_client, linked))

    assert [row[0] for row in stored(db, match.id)] == [100, 101]


def test_a_match_without_a_lobby_cannot_be_read(db, match, scan_client):
    with pytest.raises(LookupError):
        run(history.refresh_from_lobby(db, scan_client, match))


def test_a_lobby_osu_does_not_have_changes_nothing(db, cup, match, scan_client):
    events_repo.save_manual(db, match.id, event(100, side=1, source="manual"))
    linked = point_at_lobby(db, match, 4040)

    with pytest.raises(OsuNotFound):
        run(history.refresh_from_lobby(db, scan_client, linked))

    assert stored(db, match.id) == [(100, "pick", 1, None, None, "manual")]


def test_a_correction_survives_reading_the_lobby_again_and_wins_its_seq(
    db, cup, match, osu, scan_client
):
    maps = cup["maps"]
    lobby(osu, (NM1, 900, 800), (NM2, 900, 800))
    linked = point_at_lobby(db, match)
    run(history.refresh_from_lobby(db, scan_client, linked))
    events_repo.save_manual(
        db, match.id, event(100, side=2, map_id=maps["HD1"], winner=2, source="manual")
    )

    run(history.refresh_from_lobby(db, scan_client, linked))

    assert stored(db, match.id) == [
        (100, "pick", 2, maps["HD1"], 2, "manual"),  # the correction, not the lobby's NM1
        (101, "pick", 1, maps["NM2"], 1, "mp"),  # alternates from the correction
    ]


def test_wybin_bans_replace_wybin_events_and_never_touch_manual_ones(db, cup, match):
    maps = cup["maps"]
    history.record_wybin_bans(db, match.id, cup["stage"], (NM1,), (NM2,))
    events_repo.save_manual(
        db, match.id, event(2, kind="ban", side=1, map_id=maps["HD1"], source="manual")
    )

    history.record_wybin_bans(db, match.id, cup["stage"], (NM1,), (HD1,))

    assert stored(db, match.id) == [
        (1, "ban", 1, maps["NM1"], None, "wybin"),
        (2, "ban", 1, maps["HD1"], None, "manual"),  # wybin's second ban lost the seq to it
    ]


def test_wybin_bans_without_a_stage_store_nothing(db, match):
    history.record_wybin_bans(db, match.id, None, (NM1,), ())

    assert stored(db, match.id) == []


def correction(db, match, **values):
    return history.save_corrections(db, matches_repo.get(db, match.id), values)


@pytest.fixture
def played(db, cup, match):
    """Two lobby picks, as a read of the lobby leaves them."""
    maps = cup["maps"]
    events_repo.replace_source(
        db,
        match.id,
        "mp",
        [
            event(100, map_id=maps["NM1"], winner=1, beatmap=NM1),
            event(101, map_id=maps["NM2"], winner=2, beatmap=NM2),
        ],
    )
    return match


def test_naming_the_side_of_one_pick_makes_the_others_alternate(db, cup, played):
    changed = correction(
        db,
        played,
        side_100="1",
        map_100=str(cup["maps"]["NM1"]),
        side_101="",
        map_101=str(cup["maps"]["NM2"]),
    )

    assert changed
    assert [(row[0], row[2], row[5]) for row in stored(db, played.id)] == [
        (100, 1, "manual"),
        (101, 2, "mp"),
    ]


def test_undoing_the_manual_side_takes_the_sides_it_gave_the_other_picks_away(db, cup, played):
    maps = cup["maps"]
    correction(db, played, side_100="1", map_100=str(maps["NM1"]), map_101=str(maps["NM2"]))
    assert [row[2] for row in stored(db, played.id)] == [1, 2]

    correction(db, played, reset_100="1")

    # the correction replaced pick 100; undoing it leaves the lobby's pick 101, now without a side
    assert [(row[0], row[2], row[5]) for row in stored(db, played.id)] == [(101, None, "mp")]


def test_an_untouched_form_changes_nothing(db, cup, played):
    unchanged = {
        "side_100": "",
        "map_100": str(cup["maps"]["NM1"]),
        "winner_100": "1",
        "side_101": "",
        "map_101": str(cup["maps"]["NM2"]),
        "winner_101": "2",
    }

    assert correction(db, played, **unchanged) is False
    assert {row[5] for row in stored(db, played.id)} == {"mp"}


def test_changing_the_map_of_an_event_drops_the_scores_that_belonged_to_the_old_one(
    db, cup, played
):
    maps = cup["maps"]
    events_repo.replace_source(
        db, played.id, "mp", [MatchEvent(100, "pick", None, maps["NM1"], NM1, 1, 5, 4, "mp")]
    )

    correction(db, played, side_100="", map_100=str(maps["HD1"]), winner_100="1")

    fixed = events_repo.list_for_match(db, played.id)[0]
    assert (fixed.pool_map_id, fixed.beatmap_id, fixed.team1_score) == (maps["HD1"], HD1, None)


def test_undoing_a_correction_removes_it(db, cup, played):
    correction(db, played, side_100="2", map_100=str(cup["maps"]["NM1"]), winner_100="1")

    correction(db, played, reset_100="1")

    assert [row[5] for row in stored(db, played.id)] == ["mp"]  # lobby row 101 only
    assert [row[0] for row in stored(db, played.id)] == [101]


def test_a_new_ban_gets_the_next_seq_below_the_picks_and_a_new_pick_the_next_above(db, cup, played):
    maps = cup["maps"]
    correction(db, played, new_kind="ban", new_side="2", new_map=str(maps["HD1"]))
    correction(db, played, new_kind="pick", new_side="1", new_map=str(maps["HD1"]), new_winner="1")

    seqs = [(row[0], row[1], row[5]) for row in stored(db, played.id)]
    assert (1, "ban", "manual") in seqs
    assert (102, "pick", "manual") in seqs


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"side_100": "3"}, "side_100"),
        ({"side_100": "x"}, "side_100"),
        ({"map_100": "999999"}, "map_100"),
        ({"winner_100": "7"}, "winner_100"),
        ({"new_kind": "steal"}, "new_kind"),
        ({"new_kind": "ban", "new_side": "1"}, "new_map"),
        ({"new_kind": "ban", "new_map": "{NM1}"}, "new_side"),
        ({"new_kind": "pick", "new_side": "x", "new_map": "{NM1}"}, "new_side"),
    ],
)
def test_a_bad_field_is_refused_and_nothing_is_saved(db, cup, played, values, field):
    maps = cup["maps"]
    form = {"side_100": "", "map_100": str(maps["NM1"]), "winner_100": "1"}
    form.update({key: value.format(**maps) for key, value in values.items()})

    with pytest.raises(HistoryError) as refused:
        correction(db, played, **form)

    assert refused.value.field == field
    assert {row[5] for row in stored(db, played.id)} == {"mp"}


def test_a_tiebreaker_cannot_be_given_a_team_and_a_ban_cannot_have_a_winner(db, cup):
    maps = cup["maps"]
    match_id = add_game(db, cup, when="2026-11-15T18:00:00Z")
    row = matches_repo.get(db, match_id)
    events_repo.replace_source(
        db,
        match_id,
        "mp",
        [event(100, kind="tiebreaker", map_id=maps["TB"]), event(101, map_id=maps["NM1"])],
    )

    with pytest.raises(HistoryError):
        history.save_corrections(
            db, row, {"side_100": "1", "map_100": str(maps["TB"]), "winner_100": ""}
        )
    with pytest.raises(HistoryError):
        history.save_corrections(
            db,
            row,
            {"new_kind": "ban", "new_side": "1", "new_map": str(maps["NM1"]), "new_winner": "2"},
        )


# --- reading lobbies in the background ---------------------------------------------------------


def test_the_background_job_reads_each_finished_match_with_a_lobby_once(
    db, cup, match, osu, scan_client
):
    lobby(osu, (NM1, 900, 800))
    point_at_lobby(db, match)

    first = run(history.import_pending(db, scan_client))
    requests = len(osu.match_requests(777))
    second = run(history.import_pending(db, scan_client))

    assert (first, second) == (1, 0)
    assert len(osu.match_requests(777)) == requests
    assert [row[0] for row in stored(db, match.id)] == [100]


def spy_on_priority(monkeypatch):
    seen = []
    original = OsuClient.fetch_match

    async def fetch_match(self, match_id, *args, **kwargs):
        seen.append(kwargs.get("priority"))
        return await original(self, match_id, *args, **kwargs)

    monkeypatch.setattr(OsuClient, "fetch_match", fetch_match)
    return seen


def test_the_background_job_reads_at_capture_priority_and_a_person_at_interactive(
    db, cup, match, osu, scan_client, monkeypatch
):
    lobby(osu, (NM1, 900, 800))
    linked = point_at_lobby(db, match)
    seen = spy_on_priority(monkeypatch)

    run(history.import_pending(db, scan_client))
    run(history.refresh_from_lobby(db, scan_client, linked))

    assert seen == [Priority.CAPTURE, Priority.INTERACTIVE]


def test_the_job_skips_matches_that_are_not_finished_or_already_have_events(
    db, cup, match, osu, scan_client
):
    lobby(osu, (NM1, 900, 800))
    point_at_lobby(db, match)
    events_repo.save_manual(db, match.id, event(100, side=1, source="manual"))
    open_match = add_game(db, cup, when="2026-11-20T18:00:00Z")
    db.execute("UPDATE matches SET mp_match_id = 777 WHERE id = ?", (open_match,))

    assert run(history.import_pending(db, scan_client)) == 0
    assert osu.match_requests(777) == []


def test_a_lobby_osu_does_not_have_is_marked_so_it_is_not_asked_for_every_run(
    db, cup, match, scan_client, osu
):
    point_at_lobby(db, match, 4040)

    assert run(history.import_pending(db, scan_client)) == 1
    assert run(history.import_pending(db, scan_client)) == 0
    assert app_settings_repo.get(db, f"{history.READ_PREFIX}{match.id}") == "1"


def test_osu_being_down_leaves_the_match_for_the_next_run(db, cup, match, osu, scan_client):
    lobby(osu, (NM1, 900, 800))
    point_at_lobby(db, match)
    osu.unavailable_paths.add("/api/v2/matches/777")

    with pytest.raises(OsuUnavailable):
        run(history.import_pending(db, scan_client))
    osu.unavailable_paths.clear()

    assert run(history.import_pending(db, scan_client)) == 1


def test_a_lobby_osu_keeps_failing_on_is_given_up_after_three_tries_and_logged(
    db, cup, match, osu, scan_client, caplog
):
    stuck = point_at_lobby(db, match)
    osu.unavailable_paths.add("/api/v2/matches/777")
    other = add_game(db, cup, when="2026-11-01T18:00:00Z", status="done")
    osu.add_match(888)
    osu.add_game(888, NM1, [(CAPTAIN, 2), (RIVAL_A, 1)])
    db.execute("UPDATE matches SET mp_match_id = 888 WHERE id = ?", (other,))

    for _ in range(2):  # the newest match is tried first and holds the others back
        with pytest.raises(OsuUnavailable):
            run(history.import_pending(db, scan_client))
    assert stored(db, other) == []
    with caplog.at_level("WARNING"):
        read = run(history.import_pending(db, scan_client))

    assert read == 1  # the stuck match is given up on, the other one is read
    assert app_settings_repo.get(db, f"{history.FAILED_PREFIX}{stuck.id}") == "3"
    assert f"match {stuck.id}" in caplog.text
    assert [row[0] for row in stored(db, other)] == [100]
    assert run(history.import_pending(db, scan_client)) == 0


def test_the_job_reads_at_most_its_limit_per_run(db, cup, osu, scan_client):
    for index in range(3):
        match_id = add_game(db, cup, when=f"2026-11-1{index}T18:00:00Z", status="done")
        osu.add_match(800 + index)
        osu.add_game(800 + index, NM1, [(CAPTAIN, 2), (RIVAL_A, 1)])
        db.execute("UPDATE matches SET mp_match_id = ? WHERE id = ?", (800 + index, match_id))

    assert run(history.import_pending(db, scan_client, limit=2)) == 2
    assert run(history.import_pending(db, scan_client, limit=2)) == 1


# --- wybin sync feeds the bans -----------------------------------------------------------------


@pytest.fixture
def fake(wybin):
    wybin.load_defaults()
    return wybin


@pytest.fixture
def wybin_client(fake):
    async def no_sleep(seconds: float) -> None:
        return None

    return WybinClient(make_http_client(fake.transport), 0, sleep=no_sleep)


def test_a_wybin_sync_stores_the_bans_of_a_match_and_follows_changes(db, fake, wybin_client, clock):
    add_user(db, ADMIN_ID, "AdminAmy")
    played = fake.matches[9001]["stages"][0]["matches"][0]
    played["opponentOneBans"] = [910001, "junk", {"beatmapId": 910002}]
    played["opponentTwoBans"] = [{"beatmap": {"beatmapId": 910004}}, 999999]

    run(import_tournament(db, wybin_client, "synthetic-cup", ADMIN_ID, clock))
    found = db.execute(
        "SELECT e.seq, e.team_side, e.beatmap_id FROM match_events e "
        "JOIN matches m ON m.id = e.match_id WHERE m.wybin_match_id = 5004 ORDER BY e.seq"
    ).fetchall()

    # a bare id and both object shapes are read; junk and a map outside the pool are skipped
    assert [tuple(row) for row in found] == [(1, 1, 910001), (2, 2, 910004), (3, 1, 910002)]

    played["opponentOneBans"] = []
    played["opponentTwoBans"] = []
    tournament = tournaments_repo.find_by_slug(db, "synthetic-cup")
    run(sync_tournament(db, wybin_client, tournament.id, clock))

    assert db.execute("SELECT COUNT(*) FROM match_events").fetchone()[0] == 0


# --- the review --------------------------------------------------------------------------------


def snapshot(db, cup, match_id, now):
    """Save the prediction as the match page would before the match."""
    tournament = tournaments_repo.get(db, cup["id"])
    view = matchup.build_matchup(db, tournament, matches_repo.get(db, match_id))
    assert matchup.save_snapshot(db, view, now)
    return view


def played_events(db, match_id, rows):
    events_repo.replace_source(db, match_id, "mp", rows)


def test_the_review_compares_bans_picks_and_winners_with_the_saved_prediction(db, cup, clock):
    fill_scores(db)  # ours stronger everywhere
    match_id = add_game(db, cup, when="2026-11-14T18:00:00Z")
    view = snapshot(db, cup, match_id, clock.now())
    order = view.prediction.series.orders[0]
    predicted_ban = order.bans[2][0]  # what the rivals were expected to ban
    unexpected = {predicted_ban, *order.bans[1], cup["maps"]["TB"]}
    other = next(m for m in cup["maps"].values() if m not in unexpected)
    db.execute("UPDATE matches SET status='done', score1=2, score2=1 WHERE id=?", (match_id,))
    played_events(
        db,
        match_id,
        [
            event(1, "ban", 2, predicted_ban, source="wybin"),
            event(2, "ban", 1, other, source="wybin"),
            event(100, "pick", 1, order.picks[0][1], winner=1),
            event(101, "pick", 2, predicted_ban, winner=2),  # not a predicted pick of side 2
        ],
    )

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert review.has_prediction
    assert review.series_prediction == "Alpha Squad" and review.series_correct is True
    assert (review.bans.hits, review.bans.total) == (1, 2)
    assert review.bans.rate == 0.5
    assert (review.picks.hits, review.picks.total) == (1, 2)
    assert [r.expected for r in review.rows if r.event.kind == "ban"] == [True, False]
    # every map is predicted for Alpha Squad (side 1); it won the first and lost the second
    assert (review.winners.hits, review.winners.total) == (1, 2)


def test_a_flipped_match_is_reviewed_from_the_same_sides(db, cup, clock):
    fill_scores(db)
    match_id = add_game(db, cup, ours_first=False, when="2026-11-14T18:00:00Z")
    snapshot(db, cup, match_id, clock.now())
    maps = cup["maps"]
    db.execute("UPDATE matches SET status='done', score1=1, score2=2 WHERE id=?", (match_id,))
    # side 2 is Alpha Squad here: it is predicted to win every map and did win this one
    played_events(db, match_id, [event(100, "pick", 2, maps["NM1"], winner=2)])

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert review.series_prediction == "Alpha Squad"
    assert review.series_correct is True
    assert (review.winners.hits, review.winners.total) == (1, 1)
    assert review.rows[0].predicted_winner == "Alpha Squad"


def test_a_match_without_a_saved_prediction_shows_what_was_played_and_judges_nothing(db, cup):
    match_id = add_game(db, cup)
    played_events(db, match_id, [event(100, "pick", 1, cup["maps"]["NM1"], winner=1)])

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert not review.has_prediction
    assert review.series_prediction is None
    assert review.picks.total == review.bans.total == review.winners.total == 0
    assert review.picks.rate is None
    assert review.rows[0].winner == "Alpha Squad"


def test_the_prediction_used_is_the_last_one_saved_before_the_match_started(db, cup, clock):
    fill_scores(db)
    match_id = add_game(db, cup, when="2026-11-14T18:00:00Z")
    view = snapshot(db, cup, match_id, clock.now())
    late = view.match.scheduled_at.replace("18:00", "19:00")
    predictions_repo.insert_snapshot(db, match_id, "stat", late, 0.01, "0-2", "{}", "stat-2")
    played_events(db, match_id, [event(100, "pick", 1, cup["maps"]["NM1"], winner=1)])

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert review.predicted_at < view.match.scheduled_at
    assert review.series_prediction == "Alpha Squad"  # not the 1 % snapshot saved afterwards


def test_a_match_without_a_time_is_not_compared_with_a_prediction_saved_after_play_began(
    db, cup, clock
):
    fill_scores(db)
    match_id = add_game(db, cup, when="2026-11-14T18:00:00Z")
    snapshot(db, cup, match_id, clock.now())
    db.execute("UPDATE matches SET scheduled_at = NULL WHERE id = ?", (match_id,))
    played_events(db, match_id, [event(100, "pick", 1, cup["maps"]["NM1"], winner=1)])

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert not review.has_prediction  # nothing says the snapshot came before the match
    assert review.rows[0].winner == "Alpha Squad"


def test_a_match_without_a_time_uses_the_prediction_saved_before_its_first_game(db, cup, clock):
    fill_scores(db)
    match_id = add_game(db, cup, when="2026-11-14T18:00:00Z")
    snapshot(db, cup, match_id, clock.now())
    predictions_repo.insert_snapshot(
        db, match_id, "stat", "2026-11-14T19:00:00Z", 0.01, "0-2", "{}", "stat-2"
    )
    db.execute(
        "UPDATE matches SET scheduled_at = NULL, mp_match_id = 777 WHERE id = ?", (match_id,)
    )
    lobbies_repo.upsert(db, 777, "Lobby", "2026-11-14T17:50:00Z", None, NOW_ISO, True, "manual")
    lobbies_repo.insert_hit(db, 777, 1, cup["maps"]["NM1"], "2026-11-14T18:05:00Z", "")
    played_events(db, match_id, [event(100, "pick", 1, cup["maps"]["NM1"], winner=1)])

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert review.predicted_at < "2026-11-14T18:05:00Z"
    assert review.series_prediction == "Alpha Squad"  # not the 1 % snapshot saved after the game


def test_the_first_game_is_the_earliest_pool_hit_else_the_lobby_start(db, cup):
    assert lobbies_repo.first_game_at(db, 1) is None
    lobbies_repo.upsert(db, 1, "Lobby", "2026-11-14T17:50:00Z", None, NOW_ISO, True, "manual")
    assert lobbies_repo.first_game_at(db, 1) == "2026-11-14T17:50:00Z"
    lobbies_repo.insert_hit(db, 1, 2, cup["maps"]["NM2"], "2026-11-14T18:20:00Z", "")
    lobbies_repo.insert_hit(db, 1, 1, cup["maps"]["NM1"], "2026-11-14T18:05:00Z", "")
    assert lobbies_repo.first_game_at(db, 1) == "2026-11-14T18:05:00Z"


def test_an_unreadable_saved_prediction_is_ignored(db, cup, clock):
    match_id = add_game(db, cup, when="2026-11-14T18:00:00Z")
    predictions_repo.insert_snapshot(db, match_id, "stat", NOW_ISO, 0.6, "2-1", "not json", "x")
    predictions_repo.insert_snapshot(
        db, match_id, "stat", "2026-11-14T12:00:01Z", 0.6, "2-1", json.dumps({"maps": []}), "x"
    )

    review = history.build_review(
        db, tournaments_repo.get(db, cup["id"]), matches_repo.get(db, match_id)
    )

    assert not review.has_prediction


# --- habits ------------------------------------------------------------------------------------


def earlier_match(db, cup, rows, when="2026-11-01T18:00:00Z", ours_first=True):
    match_id = add_game(db, cup, when=when, ours_first=ours_first, status="done")
    played_events(db, match_id, rows)
    return match_id


def test_habits_are_the_shares_of_a_teams_own_picks_and_bans_per_bracket(db, cup):
    maps, tid = cup["maps"], cup["id"]
    earlier_match(
        db,
        cup,
        [
            event(1, "ban", 2, maps["NM1"], source="wybin"),
            event(100, "pick", 2, maps["HD1"]),
            event(101, "pick", 1, maps["NM1"]),
            event(102, "pick", 2, maps["HD1"]),
            event(103, "pick", 2, maps["NM2"]),
            event(104, "tiebreaker", None, maps["TB"]),
        ],
    )

    found = history.habits(db, tid, cup["rivals"])

    assert found.pick_rate == {"HD": pytest.approx(2 / 3), "NM": pytest.approx(1 / 3)}
    assert found.ban_rate == {"NM": 1.0}
    assert found.first_pick_rate == 1.0  # the rivals' pick was the first pick


def test_habits_count_the_team_in_either_seat(db, cup):
    maps = cup["maps"]
    earlier_match(
        db, cup, [event(100, "pick", 1, maps["HD1"])], ours_first=False
    )  # rivals = team 1

    assert history.habits(db, cup["id"], cup["rivals"]).pick_rate == {"HD": 1.0}
    assert history.habits(db, cup["id"], cup["ours"]) is None  # the pick was not ours


def test_habits_leave_out_the_match_being_predicted_and_unknown_sides(db, cup):
    maps = cup["maps"]
    current = earlier_match(
        db, cup, [event(100, "pick", 2, maps["HD1"])], when="2026-11-14T18:00:00Z"
    )
    earlier_match(db, cup, [event(100, "pick", None, maps["NM1"])])

    assert history.habits(db, cup["id"], cup["rivals"], exclude_match_id=current) is None
    assert history.habits(db, cup["id"], cup["rivals"]).pick_rate == {"HD": 1.0}


def test_habits_leave_out_matches_scheduled_at_the_cutoff_or_later(db, cup):
    maps = cup["maps"]
    earlier_match(db, cup, [event(100, "pick", 2, maps["HD1"])], when="2026-11-01T18:00:00Z")
    earlier_match(db, cup, [event(100, "pick", 2, maps["NM1"])], when="2026-11-14T18:00:00Z")
    earlier_match(db, cup, [event(100, "pick", 2, maps["NM2"])], when="2026-11-20T18:00:00Z")

    found = history.habits(db, cup["id"], cup["rivals"], before="2026-11-14T18:00:00Z")

    assert found.pick_rate == {"HD": 1.0}
    assert history.habits(db, cup["id"], cup["rivals"]).pick_rate == {
        "HD": pytest.approx(1 / 3),
        "NM": pytest.approx(2 / 3),
    }


def test_the_match_page_does_not_learn_from_matches_played_after_it(db, cup):
    fill_scores(db)
    db.execute("UPDATE stages SET bans = 0 WHERE id = ?", (cup["stage"],))
    tournament = tournaments_repo.get(db, cup["id"])
    upcoming = add_game(db, cup, when="2026-11-14T18:00:00Z")
    before = matchup.build_matchup(db, tournament, matches_repo.get(db, upcoming))
    earlier_match(db, cup, [event(100, "pick", 2, cup["maps"]["HD1"])], when="2026-11-20T18:00:00Z")

    after = matchup.build_matchup(db, tournament, matches_repo.get(db, upcoming))

    assert first_rival_pick(after) == first_rival_pick(before)


def test_a_team_with_no_history_has_no_habits(db, cup):
    assert history.habits(db, cup["id"], cup["rivals"]) is None


def test_only_the_first_pick_of_a_match_says_who_picked_first(db, cup):
    maps = cup["maps"]
    # the first pick has no known side; the later pick of the rivals must not count as the first
    earlier_match(
        db, cup, [event(100, "pick", None, maps["NM1"]), event(101, "pick", 2, maps["HD1"])]
    )

    found = history.habits(db, cup["id"], cup["rivals"])
    view = history.build_opponent(
        db, tournaments_repo.get(db, cup["id"]), teams_repo.get(db, cup["rivals"])
    )

    assert found.first_pick_rate is None
    assert found.pick_rate == {"HD": 1.0}
    assert view.first_pick_rate is None
    assert view.rounds[0].first_pick == "unknown"


def test_first_pick_rate_is_per_match(db, cup):
    maps = cup["maps"]
    earlier_match(db, cup, [event(100, "pick", 2, maps["NM1"])])
    earlier_match(db, cup, [event(100, "pick", 1, maps["NM1"])], when="2026-11-02T18:00:00Z")

    assert history.habits(db, cup["id"], cup["rivals"]).first_pick_rate == 0.5


def first_rival_pick(view):
    """The map the rivals pick when they pick first, in the simulated series."""
    order = next(o for o in view.prediction.series.orders if o.first_pick == 2)
    return next(map_id for side, map_id in order.picks if side == 2)


def test_history_bends_the_predicted_picks_of_the_match_page(db, cup):
    fill_scores(db)  # every map is worth the same to both teams, so nothing else decides
    db.execute("UPDATE stages SET bans = 0 WHERE id = ?", (cup["stage"],))  # three maps stay
    tournament = tournaments_repo.get(db, cup["id"])
    upcoming = add_game(db, cup, when="2026-11-14T18:00:00Z")
    before = matchup.build_matchup(db, tournament, matches_repo.get(db, upcoming))
    earlier_match(db, cup, [event(100, "pick", 2, cup["maps"]["HD1"])])

    after = matchup.build_matchup(db, tournament, matches_repo.get(db, upcoming))

    assert first_rival_pick(before) != cup["maps"]["HD1"]
    assert first_rival_pick(after) == cup["maps"]["HD1"]


def test_pickems_ask_for_the_habits_of_both_teams_without_the_match_itself(db, cup, monkeypatch):
    fill_scores(db)
    tournament = tournaments_repo.get(db, cup["id"])
    stage = stages_repo.list_for_tournament(db, cup["id"])[0]
    match_id = add_game(db, cup, when="2026-11-14T18:00:00Z")
    asked = []
    original = history.habits

    def spy(conn, tournament_id, team_id, exclude_match_id=None, before=None):
        asked.append((team_id, exclude_match_id, before))
        return original(conn, tournament_id, team_id, exclude_match_id, before)

    monkeypatch.setattr(history, "habits", spy)

    matchup.build_pickems(db, tournament, stage)

    when = "2026-11-14T18:00:00Z"
    assert sorted(asked) == sorted([(cup["ours"], match_id, when), (cup["rivals"], match_id, when)])


def test_the_model_version_names_the_habit_model():
    assert prediction.MODEL_VERSION == "stat-2"
    assert prediction.HABIT_WEIGHT == 0.3


# --- the opponent view -------------------------------------------------------------------------


def test_the_opponent_view_lists_picks_bans_rounds_and_maps_never_picked(db, cup):
    maps = cup["maps"]
    earlier_match(
        db,
        cup,
        [
            event(1, "ban", 2, maps["NM1"], source="wybin"),
            event(100, "pick", 2, maps["HD1"]),
            event(101, "pick", 1, maps["NM1"]),
        ],
    )
    tournament = tournaments_repo.get(db, cup["id"])
    rivals = teams_repo.get(db, cup["rivals"])

    view = history.build_opponent(db, tournament, rivals)

    assert view.matches == 1
    assert [(b.bracket, b.picks, b.bans) for b in view.brackets] == [("HD", 1, 0), ("NM", 0, 1)]
    assert view.brackets[0].pick_share == 1.0
    assert [(r.stage, r.picked, r.banned, r.first_pick) for r in view.rounds] == [
        ("Round of 16", [("HD1", 1)], [("NM1", 1)], "1 of 1")
    ]
    assert view.first_pick_rate == 1.0
    stage_name, never = view.never_picked[0]
    assert stage_name == "Round of 16"
    assert [m.slot for m in never] == ["NM1", "NM2"]  # the tiebreaker is nobody's pick


def test_the_opponent_view_is_empty_for_a_team_without_events(db, cup):
    rivals = teams_repo.get(db, cup["rivals"])

    view = history.build_opponent(db, tournaments_repo.get(db, cup["id"]), rivals)

    assert (view.matches, view.brackets, view.rounds, view.never_picked) == (0, [], [], [])
    assert view.first_pick_rate is None


def test_two_players_of_the_rivals_are_found_on_their_roster(db, cup):
    # RIVAL_A and RIVAL_B sit on team "Rival Crew", MATE on ours: the roster decides the side
    members = history._membership(db, matches_repo.get(db, add_game(db, cup)))

    assert members[CAPTAIN] == members[MATE] == 1
    assert members[RIVAL_A] == members[RIVAL_B] == 2
