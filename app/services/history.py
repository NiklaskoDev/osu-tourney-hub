"""What really happened in a match, and how the prediction compared.

Events (protects, bans, picks, with the winner of each map) come from three sources: the bans
wybin lists, the games of the match's multiplayer lobby, and a captain's corrections. A correction
(`source='manual'`) always wins over what an import derives for the same `seq`, and an import never
touches it. Bans take seqs from 1, picks from `PICK_SEQ_START`, so a correction keeps its place
when the bans change.

The lobby does not say who picked a map. Picks therefore start without a side; once a captain names
the side of one pick, the other picks are taken to alternate from it.
"""

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.clients.osu import OsuClient, OsuError, OsuNotFound
from app.clients.ratelimit import Priority
from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import Match, MpGame, PoolMap, Stage, Team, Tournament
from app.domain.parsing import parse_uint
from app.repos import app_settings as app_settings_repo
from app.repos import lobbies as lobbies_repo
from app.repos import match_events as events_repo
from app.repos import matches as matches_repo
from app.repos import predictions as predictions_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos.match_events import MatchEvent
from app.services import mp_parse
from app.services.prediction import Habits

logger = logging.getLogger(__name__)

PICK_SEQ_START = 100
SIDES = (1, 2)
EVENT_KINDS = ("protect", "ban", "pick", "tiebreaker")
KIND_LABELS = {"protect": "Protect", "ban": "Ban", "pick": "Pick", "tiebreaker": "Tiebreaker"}
SIDE_KINDS = ("protect", "ban", "pick")  # a tiebreaker is nobody's choice
READ_PREFIX = "history_read:"  # app_settings marker: this match's lobby was read once
FAILED_PREFIX = "history_failed:"  # app_settings counter: osu! failed to give this match's lobby
MAX_FAILED_READS = 3
LOBBIES_PER_RUN = 5


class HistoryError(Exception):
    """A refused correction; `field` names the input to mark."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


# --- deriving events ---------------------------------------------------------------------------


def wybin_events(
    pool: list[PoolMap], bans1: tuple[int, ...], bans2: tuple[int, ...]
) -> list[MatchEvent]:
    """The bans wybin lists, interleaved side 1, side 2 (wybin keeps one list per team, so the
    real order between teams is not known). A ban that is not a map of the pool is skipped."""
    by_beatmap = {m.beatmap_id: m for m in pool if m.beatmap_id is not None}
    events: list[MatchEvent] = []
    for index in range(max(len(bans1), len(bans2))):
        for side, bans in ((1, bans1), (2, bans2)):
            pool_map = by_beatmap.get(bans[index]) if index < len(bans) else None
            if pool_map is not None:
                events.append(
                    MatchEvent(
                        len(events) + 1,
                        "ban",
                        side,
                        pool_map.id,
                        pool_map.beatmap_id,
                        None,
                        None,
                        None,
                        "wybin",
                    )
                )
    return events


def derive_mp_events(
    games: list[MpGame], pool: list[PoolMap], membership: Mapping[int, int]
) -> list[MatchEvent]:
    """One pick per map played, in play order. `membership` maps a player's osu! id to the side
    (1 or 2) of the team they belong to.

    A game without scores was aborted. A map played again counts once, at its first place, with
    the result of the last game. With a pool known, games on maps outside it are warm-ups and
    skipped."""
    by_beatmap = {m.beatmap_id: m for m in pool if m.beatmap_id is not None}
    played: dict[int, MpGame] = {}
    for game in games:
        if not game.scores or (by_beatmap and game.beatmap_id not in by_beatmap):
            continue
        played[game.beatmap_id] = game
    events = []
    for index, (beatmap_id, game) in enumerate(played.items()):
        pool_map = by_beatmap.get(beatmap_id)
        totals = _side_totals(game, membership)
        events.append(
            MatchEvent(
                PICK_SEQ_START + index,
                "tiebreaker" if pool_map and pool_map.is_tiebreaker else "pick",
                None,
                pool_map.id if pool_map else None,
                beatmap_id,
                _winner(totals),
                totals[1] if totals else None,
                totals[2] if totals else None,
                "mp",
            )
        )
    return events


def _side_totals(game: MpGame, membership: Mapping[int, int]) -> dict[int, int] | None:
    """The summed score of each side, or None when no score could be given to a side. A player is
    given the side of their team; one who is on neither roster (a stand-in) gets the side the team
    colour stands for, learned from the players whose team is known."""
    colour_side = _colour_sides(game, membership)
    totals = {1: 0, 2: 0}
    attributed = False
    for score in game.scores:
        side = membership.get(score.osu_id) or colour_side.get(score.team or "")
        if side is not None:
            totals[side] += score.score
            attributed = True
    return totals if attributed else None


def _colour_sides(game: MpGame, membership: Mapping[int, int]) -> dict[str, int]:
    votes: dict[str, Counter[int]] = {}
    for score in game.scores:
        side = membership.get(score.osu_id)
        if side is not None and score.team:
            votes.setdefault(score.team, Counter())[side] += 1
    chosen: dict[str, int] = {}
    for colour, counted in votes.items():
        ranked = counted.most_common()
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            chosen[colour] = ranked[0][0]
    if len(set(chosen.values())) < len(chosen):  # both colours on one side: the mapping is wrong
        return {}
    return chosen


def _winner(totals: dict[int, int] | None) -> int | None:
    if totals is None or totals[1] == totals[2]:
        return None
    return 1 if totals[1] > totals[2] else 2


def assign_pick_sides(events: list[MatchEvent]) -> list[MatchEvent]:
    """Picks alternate between the sides. The first pick a captain gave a side is the anchor; the
    picks an import derived take their side from it. Without an anchor they have no side."""
    picks = [e for e in sorted(events, key=lambda e: e.seq) if e.kind == "pick"]
    anchor = next(
        (i for i, e in enumerate(picks) if e.source == "manual" and e.team_side is not None), None
    )
    if anchor is None:
        return [
            replace_side(e, None) if e.kind == "pick" and e.source != "manual" else e
            for e in events
        ]
    anchor_side = picks[anchor].team_side or 1  # the anchor has a side by the search above
    sides = {
        e.seq: anchor_side if (i - anchor) % 2 == 0 else 3 - anchor_side
        for i, e in enumerate(picks)
        if e.source != "manual"
    }
    return [replace_side(e, sides[e.seq]) if e.seq in sides else e for e in events]


def replace_side(event: MatchEvent, side: int | None) -> MatchEvent:
    return MatchEvent(
        event.seq,
        event.kind,
        side,
        event.pool_map_id,
        event.beatmap_id,
        event.winner_side,
        event.team1_score,
        event.team2_score,
        event.source,
    )


# --- storing events ----------------------------------------------------------------------------


def record_wybin_bans(
    conn: sqlite3.Connection,
    match_id: int,
    stage_id: int | None,
    bans1: tuple[int, ...],
    bans2: tuple[int, ...],
) -> None:
    """Make the match's wybin events what wybin lists now. Runs inside the caller's transaction
    (the wybin sync's)."""
    pool = stages_repo.list_maps(conn, stage_id) if stage_id is not None else []
    events_repo.replace_source(conn, match_id, "wybin", wybin_events(pool, bans1, bans2))
    _realign(conn, match_id)


def _realign(conn: sqlite3.Connection, match_id: int) -> None:
    """Give the derived picks their alternating sides from the stored events. Without a manual
    side to anchor them (a captain undid it) they go back to no side."""
    stored = events_repo.list_for_match(conn, match_id)
    for before, after in zip(stored, assign_pick_sides(stored), strict=True):
        if before.team_side != after.team_side:
            events_repo.set_side(conn, match_id, after.seq, after.team_side)


def _membership(conn: sqlite3.Connection, match: Match) -> dict[int, int]:
    members: dict[int, int] = {}
    for side, team_id in ((1, match.team1_id), (2, match.team2_id)):
        if team_id is not None:
            for player in teams_repo.list_players(conn, team_id):
                members[player.osu_id] = side
    return members


async def refresh_from_lobby(
    conn: sqlite3.Connection,
    osu: OsuClient,
    match: Match,
    now: datetime,
    priority: Priority = Priority.INTERACTIVE,
) -> int:
    """Read the match's lobby and make its lobby events what the games show. Returns how many
    events the lobby gave. Raises `OsuNotFound` or another `OsuError` when osu! cannot give the
    lobby, and `LookupError` for a match without one. A person waiting for the page reads at
    the default priority; the background job passes a lower one."""
    if match.mp_match_id is None:
        raise LookupError("match has no lobby")
    fetched = await osu.fetch_match(match.mp_match_id, priority=priority)
    if fetched.truncated:
        logger.warning("Lobby %s has more events than were read", match.mp_match_id)
    return store_lobby_events(conn, match, fetched.events, now)


def store_lobby_events(
    conn: sqlite3.Connection, match: Match, lobby_events: list[dict], now: datetime
) -> int:
    """Make the match's lobby events what the raw osu! events of its lobby show, and keep the
    players' scores of its pool games (the match stats page shows them). Returns how many events
    that gave."""
    pool = stages_repo.list_maps(conn, match.stage_id) if match.stage_id is not None else []
    membership = _membership(conn, match)
    games = mp_parse.parse_games(lobby_events)
    events = derive_mp_events(games, pool, membership)
    with transaction(conn):
        events_repo.replace_source(conn, match.id, "mp", events)
        _realign(conn, match.id)
        _store_player_scores(conn, match, games, pool, membership, to_utc_iso(now))
    return len(events)


def _store_player_scores(
    conn: sqlite3.Connection,
    match: Match,
    games: list[MpGame],
    pool: list[PoolMap],
    membership: Mapping[int, int],
    created_at: str,
) -> None:
    """The scores of both teams' rostered players on pool maps (every map when the pool is not
    known). Stored once per game and player, so reading a lobby again adds nothing."""
    if match.mp_match_id is None:
        return
    in_pool = {m.beatmap_id for m in pool if m.beatmap_id is not None}
    for game in games:
        if not game.scores or (in_pool and game.beatmap_id not in in_pool):
            continue
        for score in game.scores:
            if score.osu_id in membership:
                scores_repo.insert_mp(
                    conn,
                    match.mp_match_id,
                    game.game_id,
                    game.beatmap_id,
                    game.gamemode,
                    game.scoring,
                    game.played_at,
                    score,
                    created_at,
                )


async def import_pending(
    conn: sqlite3.Connection, osu: OsuClient, now: datetime, limit: int = LOBBIES_PER_RUN
) -> int:
    """Read the lobbies of finished matches that have none read yet, a few per run, each once (a
    person can read one again from the review page). When osu! fails the error is raised and the
    match is tried again next run, but only `MAX_FAILED_READS` times: then it is given up on, so
    one lobby osu! cannot give does not hold up the others. Returns how many lobbies were read."""
    read = 0
    for match in matches_repo.list_played_with_lobby(conn, limit, READ_PREFIX):
        try:
            await refresh_from_lobby(conn, osu, match, now, Priority.CAPTURE)
        except OsuNotFound:
            logger.info("Match %s names a lobby osu! does not have", match.id)
        except OsuError:
            if not _count_failure(conn, match.id):
                raise
            logger.warning("Giving up on the lobby of match %s: osu! failed to give it", match.id)
            continue
        app_settings_repo.put(conn, f"{READ_PREFIX}{match.id}", "1")
        read += 1
    return read


def _count_failure(conn: sqlite3.Connection, match_id: int) -> bool:
    """Count one failed read; returns whether the match has now failed too often to try again
    (it is marked as read then)."""
    key = f"{FAILED_PREFIX}{match_id}"
    failures = int(app_settings_repo.get(conn, key) or 0) + 1
    app_settings_repo.put(conn, key, str(failures))
    if failures < MAX_FAILED_READS:
        return False
    app_settings_repo.put(conn, f"{READ_PREFIX}{match_id}", "1")
    return True


# --- manual corrections ------------------------------------------------------------------------


def side_field(seq: int) -> str:
    return f"side_{seq}"


def map_field(seq: int) -> str:
    return f"map_{seq}"


def winner_field(seq: int) -> str:
    return f"winner_{seq}"


def reset_field(seq: int) -> str:
    return f"reset_{seq}"


NEW_FIELDS = ("new_kind", "new_side", "new_map", "new_winner")


def save_corrections(conn: sqlite3.Connection, match: Match, values: Mapping[str, str]) -> bool:
    """Apply the correction form: every event whose side, map or winner differs from what is
    stored becomes a manual event, a ticked "reset" removes a manual event, and a filled "new"
    row adds one. Nothing is stored when any field is refused. Returns whether anything changed."""
    pool = {m.id: m for m in _pool(conn, match)}
    stored = {e.seq: e for e in events_repo.list_for_match(conn, match.id)}
    changes: list[MatchEvent] = []
    resets: list[int] = []
    for seq, event in stored.items():
        if values.get(reset_field(seq)) and event.source == "manual":
            resets.append(seq)
            continue
        if side_field(seq) not in values or map_field(seq) not in values:
            continue  # the row was not on the form
        edited = _edited(event, values, pool, seq)
        if (edited.team_side, edited.pool_map_id, edited.winner_side) != (
            event.team_side,
            event.pool_map_id,
            event.winner_side,
        ):
            changes.append(edited)
    added = _new_event(values, pool, stored)
    if added is not None:
        changes.append(added)
    if not changes and not resets:
        return False
    with transaction(conn):
        for seq in resets:
            events_repo.delete_manual(conn, match.id, seq)
        for event in changes:
            events_repo.save_manual(conn, match.id, event)
        _realign(conn, match.id)
    return True


def _pool(conn: sqlite3.Connection, match: Match) -> list[PoolMap]:
    return stages_repo.list_maps(conn, match.stage_id) if match.stage_id is not None else []


def _side(text: str, field: str) -> int | None:
    if text.strip() == "":
        return None
    side = parse_uint(text)
    if side not in SIDES:
        raise HistoryError("Choose one of the two teams, or leave it empty.", field)
    return side


def _map(text: str, pool: Mapping[int, PoolMap], field: str) -> PoolMap | None:
    if text.strip() == "":
        return None
    pool_map_id = parse_uint(text)
    if pool_map_id not in pool:
        raise HistoryError("Choose a map from this stage's pool.", field)
    return pool[pool_map_id]


def _edited(
    event: MatchEvent, values: Mapping[str, str], pool: Mapping[int, PoolMap], seq: int
) -> MatchEvent:
    side = _side(values.get(side_field(seq), ""), side_field(seq))
    shown = winner_field(seq) in values  # the form has no winner for a ban or a protect
    winner = _side(values[winner_field(seq)], winner_field(seq)) if shown else event.winner_side
    chosen = _map(values[map_field(seq)], pool, map_field(seq))
    if event.kind == "tiebreaker" and side is not None:
        raise HistoryError("A tiebreaker is not picked by a team.", side_field(seq))
    if event.kind in ("protect", "ban") and winner is not None:
        raise HistoryError("Only a played map has a winner.", winner_field(seq))
    pool_map_id = chosen.id if chosen else None
    same_map = pool_map_id == event.pool_map_id
    return MatchEvent(
        seq,
        event.kind,
        side,
        pool_map_id,
        event.beatmap_id if same_map else (chosen.beatmap_id if chosen else None),
        winner,
        event.team1_score if same_map else None,
        event.team2_score if same_map else None,
        "manual",
    )


def _new_event(
    values: Mapping[str, str], pool: Mapping[int, PoolMap], stored: Mapping[int, MatchEvent]
) -> MatchEvent | None:
    kind = values.get("new_kind", "").strip()
    if kind == "":
        return None
    if kind not in EVENT_KINDS:
        raise HistoryError("Choose what kind of event this is.", "new_kind")
    side = _side(values.get("new_side", ""), "new_side")
    winner = _side(values.get("new_winner", ""), "new_winner")
    chosen = _map(values.get("new_map", ""), pool, "new_map")
    if chosen is None:
        raise HistoryError("Choose the map.", "new_map")
    if kind in ("protect", "ban") and side is None:
        raise HistoryError("Choose which team made it.", "new_side")
    if kind == "tiebreaker" and side is not None:
        raise HistoryError("A tiebreaker is not picked by a team.", "new_side")
    if kind in ("protect", "ban") and winner is not None:
        raise HistoryError("Only a played map has a winner.", "new_winner")
    return MatchEvent(
        _next_seq(kind, stored),
        kind,
        side,
        chosen.id,
        chosen.beatmap_id,
        winner,
        None,
        None,
        "manual",
    )


def _next_seq(kind: str, stored: Mapping[int, MatchEvent]) -> int:
    if kind in ("pick", "tiebreaker"):
        return max([PICK_SEQ_START - 1, *stored]) + 1
    below = [seq for seq in stored if seq < PICK_SEQ_START]
    seq = max(below, default=0) + 1
    if seq >= PICK_SEQ_START:
        raise HistoryError("There is no room for more protects and bans.", "new_kind")
    return seq


# --- the review: actual against predicted ------------------------------------------------------


@dataclass(frozen=True)
class Tally:
    hits: int
    total: int

    @property
    def rate(self) -> float | None:
        return self.hits / self.total if self.total else None


@dataclass(frozen=True)
class ReviewRow:
    event: MatchEvent
    pool_map: PoolMap | None
    team: str | None  # who made the choice
    expected: (
        bool | None
    )  # a protect, ban or pick: whether the prediction named it (None: no way to tell)
    winner: str | None
    predicted_winner: str | None
    winner_correct: bool | None


@dataclass(frozen=True)
class ReviewView:
    match: Match
    stage: Stage | None
    team1: Team | None
    team2: Team | None
    rows: list[ReviewRow]
    predicted_at: str | None  # when the prediction used was saved
    series_prediction: str | None  # the team the prediction favoured
    series_correct: bool | None
    protects: Tally
    bans: Tally
    picks: Tally
    winners: Tally
    pool: list[PoolMap]

    @property
    def has_prediction(self) -> bool:
        return self.predicted_at is not None


@dataclass(frozen=True)
class _Forecast:
    """The saved prediction, with the sides turned into the match's own (1 = team 1)."""

    created_at: str
    win_prob_team1: float
    map_favours: dict[int, int | None]  # pool map id -> side expected to win it
    chosen: dict[str, dict[int, set[int]]]  # kind -> side -> pool map ids (per first-pick order)
    orders: list[tuple[int, dict[str, dict[int, set[int]]]]]


def _forecast(match: Match, stored: predictions_repo.Snapshot) -> _Forecast | None:
    try:
        payload = json.loads(stored.payload)
        flip = payload["side1_team_id"] != match.team1_id

        def side_of(payload_side: int) -> int:
            return 3 - payload_side if flip else payload_side

        favours: dict[int, int | None] = {}
        for item in payload["maps"]:
            prob = item["prob_side1"]
            favours[item["pool_map_id"]] = (
                None if prob is None or prob == 0.5 else side_of(1 if prob > 0.5 else 2)
            )
        orders = []
        for order in payload["orders"]:
            chosen: dict[str, dict[int, set[int]]] = {"protect": {}, "ban": {}, "pick": {}}
            for kind, key in (("protect", "protects"), ("ban", "bans")):
                for side, ids in order[key].items():
                    chosen[kind][side_of(int(side))] = set(ids)
            for side, pool_map_id in order["picks"]:
                chosen["pick"].setdefault(side_of(side), set()).add(pool_map_id)
            orders.append((side_of(order["first_pick"]), chosen))
    except (ValueError, KeyError, TypeError):
        logger.warning("Prediction of match %s could not be read", match.id)
        return None
    if not orders:
        return None
    return _Forecast(stored.created_at, stored.win_prob_team1, favours, orders[0][1], orders)


def build_review(conn: sqlite3.Connection, tournament: Tournament, match: Match) -> ReviewView:
    teams = {t.id: t for t in teams_repo.list_for_tournament(conn, tournament.id)}
    team1 = teams.get(match.team1_id) if match.team1_id is not None else None
    team2 = teams.get(match.team2_id) if match.team2_id is not None else None
    stage = stages_repo.get(conn, match.stage_id) if match.stage_id is not None else None
    pool = _pool(conn, match)
    by_id = {m.id: m for m in pool}
    events = sorted(events_repo.list_for_match(conn, match.id), key=_display_order)
    snapshot = _snapshot_before_play(conn, match)
    forecast = _forecast(match, snapshot) if snapshot else None
    names = {1: team1.name if team1 else None, 2: team2.name if team2 else None}
    first_picker = next((e.team_side for e in events if e.kind == "pick" and e.team_side), None)
    if forecast is not None and first_picker is not None:
        chosen = next((c for first, c in forecast.orders if first == first_picker), forecast.chosen)
    else:
        chosen = forecast.chosen if forecast else {}
    rows = [_review_row(e, by_id, names, forecast, chosen) for e in events]
    series_prediction, series_correct = _series(match, forecast, names)
    return ReviewView(
        match,
        stage,
        team1,
        team2,
        rows,
        forecast.created_at if forecast else None,
        series_prediction,
        series_correct,
        _tally(rows, "protect"),
        _tally(rows, "ban"),
        _tally(rows, "pick"),
        Tally(
            sum(1 for r in rows if r.winner_correct),
            sum(1 for r in rows if r.winner_correct is not None),
        ),
        pool,
    )


def _snapshot_before_play(
    conn: sqlite3.Connection, match: Match
) -> predictions_repo.Snapshot | None:
    """The last prediction saved before the match began: at its scheduled time, else at its first
    game. With neither known there is no telling which prediction came before play, so none."""
    began = match.scheduled_at
    if began is None and match.mp_match_id is not None:
        began = lobbies_repo.first_game_at(conn, match.mp_match_id)
    if began is None:
        return None
    return predictions_repo.latest_snapshot(conn, match.id, "stat", began)


def _display_order(event: MatchEvent) -> tuple[int, int]:
    rank = {"protect": 0, "ban": 1}.get(event.kind, 2)
    return rank, event.seq


def _review_row(
    event: MatchEvent,
    by_id: Mapping[int, PoolMap],
    names: Mapping[int, str | None],
    forecast: _Forecast | None,
    chosen: Mapping[str, Mapping[int, set[int]]],
) -> ReviewRow:
    expected = None
    if (
        forecast is not None
        and event.kind in SIDE_KINDS
        and event.team_side is not None
        and event.pool_map_id is not None
    ):
        expected = event.pool_map_id in chosen[event.kind].get(event.team_side, set())
    favoured = (
        forecast.map_favours.get(event.pool_map_id)
        if forecast is not None and event.pool_map_id is not None
        else None
    )
    played = event.kind in ("pick", "tiebreaker")
    correct = favoured == event.winner_side if played and favoured and event.winner_side else None
    return ReviewRow(
        event,
        by_id.get(event.pool_map_id) if event.pool_map_id is not None else None,
        names.get(event.team_side) if event.team_side else None,
        expected,
        names.get(event.winner_side) if event.winner_side else None,
        names.get(favoured) if favoured and played else None,
        correct,
    )


def _tally(rows: list[ReviewRow], kind: str) -> Tally:
    judged = [r.expected for r in rows if r.event.kind == kind and r.expected is not None]
    return Tally(sum(judged), len(judged))


def _series(
    match: Match, forecast: _Forecast | None, names: Mapping[int, str | None]
) -> tuple[str | None, bool | None]:
    if forecast is None or forecast.win_prob_team1 == 0.5:
        return None, None
    favoured = 1 if forecast.win_prob_team1 > 0.5 else 2
    result = None
    if match.status == "done" and match.score1 is not None and match.score2 is not None:
        if match.score1 != match.score2:
            result = 1 if match.score1 > match.score2 else 2
    return names.get(favoured), (result == favoured if result else None)


# --- habits and the opponent page --------------------------------------------------------------


def habits(
    conn: sqlite3.Connection,
    tournament_id: int,
    team_id: int,
    exclude_match_id: int | None = None,
    before: str | None = None,
) -> Habits | None:
    """How the team has picked and banned in the matches played so far (the one named left out,
    and none scheduled at `before` or later): the share of its picks and of its bans that were on
    each bracket, and how often it picked first. None when there is nothing to go on."""
    events = events_repo.list_for_team(conn, tournament_id, team_id, exclude_match_id, before)
    pick_rate = _shares(e.bracket for e in events if e.kind == "pick" and e.own and e.bracket)
    ban_rate = _shares(e.bracket for e in events if e.kind == "ban" and e.own and e.bracket)
    if not pick_rate and not ban_rate:
        return None
    return Habits(pick_rate, ban_rate, _first_pick_rate(events))


def _shares(brackets) -> dict[str, float]:
    counted = Counter(brackets)
    total = sum(counted.values())
    return {bracket: count / total for bracket, count in counted.items()}


def _first_picks(events: list[events_repo.TeamEvent]) -> dict[int, bool]:
    """Per match, whether the team made the first pick. A match whose first pick has no known
    side is left out: a later pick says nothing about who started."""
    first: dict[int, events_repo.TeamEvent] = {}
    for event in events:  # in match, then event order
        if event.kind == "pick":
            first.setdefault(event.match_id, event)
    return {match_id: e.own for match_id, e in first.items() if e.own is not None}


def _first_pick_rate(events: list[events_repo.TeamEvent]) -> float | None:
    first = _first_picks(events)
    return sum(first.values()) / len(first) if first else None


@dataclass(frozen=True)
class BracketHabit:
    bracket: str
    picks: int
    pick_share: float
    bans: int
    ban_share: float


@dataclass(frozen=True)
class RoundHabit:
    stage: str
    matches: int
    picked: list[tuple[str, int]]  # slot, times
    banned: list[tuple[str, int]]
    first_pick: str  # "2 of 3", or "unknown"


@dataclass(frozen=True)
class OpponentView:
    team: Team
    matches: int  # matches with events
    brackets: list[BracketHabit]
    rounds: list[RoundHabit]
    first_pick_rate: float | None
    never_picked: list[tuple[str, list[PoolMap]]]  # stage name, maps the team never picked there


def build_opponent(conn: sqlite3.Connection, tournament: Tournament, team: Team) -> OpponentView:
    events = events_repo.list_for_team(conn, tournament.id, team.id)
    stages = {s.id: s for s in stages_repo.list_for_tournament(conn, tournament.id)}
    own_picks = [e for e in events if e.kind == "pick" and e.own]
    own_bans = [e for e in events if e.kind == "ban" and e.own]
    pick_counts = Counter(e.bracket for e in own_picks if e.bracket)
    ban_counts = Counter(e.bracket for e in own_bans if e.bracket)
    pick_share = _shares(pick_counts.elements())
    ban_share = _shares(ban_counts.elements())
    brackets = [
        BracketHabit(
            b, pick_counts[b], pick_share.get(b, 0.0), ban_counts[b], ban_share.get(b, 0.0)
        )
        for b in sorted({*pick_counts, *ban_counts})
    ]
    rounds, never = [], []
    for stage_id in sorted(
        {e.stage_id for e in events if e.stage_id}, key=lambda s: stages[s].sort_order
    ):
        in_stage = [e for e in events if e.stage_id == stage_id]
        rounds.append(_round(stages[stage_id], in_stage))
        picked = {e.pool_map_id for e in in_stage if e.kind == "pick" and e.own}
        unpicked = [
            m
            for m in stages_repo.list_maps(conn, stage_id)
            if m.id not in picked and not m.is_tiebreaker
        ]
        if any(e.kind == "pick" and e.own is not None for e in in_stage) and unpicked:
            never.append((stages[stage_id].name, unpicked))
    return OpponentView(
        team, len({e.match_id for e in events}), brackets, rounds, _first_pick_rate(events), never
    )


def _round(stage: Stage, events: list[events_repo.TeamEvent]) -> RoundHabit:
    def slots(kind: str) -> list[tuple[str, int]]:
        counted = Counter(e.slot for e in events if e.kind == kind and e.own and e.slot)
        return sorted(counted.items())

    first = _first_picks(events)
    text = f"{sum(first.values())} of {len(first)}" if first else "unknown"
    return RoundHabit(
        stage.name, len({e.match_id for e in events}), slots("pick"), slots("ban"), text
    )
