"""The optional AI estimate of a match: what is sent to the provider, and how the answer is kept.

Only data the page already shows goes out: the stage format, the pool slots with their star rating
and mods, the expected score of each team per map, each player's best scores, the statistical
model's output and the teams' pick and ban habits. Unless an admin allows it (`ai_share_usernames`),
no osu! username and no team name leaves the server: teams are "Team A" and "Team B", players are
"Player A", "Player B" and so on.
"""

import json
import sqlite3
import string
from dataclasses import dataclass
from datetime import datetime

from app.clients.ai import AiProvider
from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.ai import SIDE_A, SIDE_B, AiPrediction, PredictionContext
from app.domain.models import Match, PoolMap, Stage, Team, Tournament
from app.repos import app_settings as app_settings_repo
from app.repos import members as members_repo
from app.repos import predictions as predictions_repo
from app.repos import teams as teams_repo
from app.services import history, matchup, prediction
from app.services.prediction import Habits
from app.services.schedule import UNKNOWN_TEAM
from app.services.tournament_settings import effective_lineup_size

SHARE_USERNAMES_KEY = "ai_share_usernames"
KIND = "ai"
CANNOT_PREDICT = "There is nothing to estimate for this match yet."


class CannotPredict(Exception):
    """The match has no teams, stage or published pool, so there is nothing to send."""


@dataclass(frozen=True)
class AiEstimate:
    """A stored estimate as the match page shows it, in the page's team names."""

    created_at: str
    model: str
    left_chance: float  # of the page's left team
    winner_name: str
    win_percent: float
    bans: list[tuple[str, list[str]]]  # (team name, slots)
    picks: list[tuple[str, str]]  # (team name, slot) in pick order
    reasoning: str


def share_usernames(conn: sqlite3.Connection) -> bool:
    return app_settings_repo.get(conn, SHARE_USERNAMES_KEY) == "1"


def set_share_usernames(conn: sqlite3.Connection, share: bool) -> None:
    app_settings_repo.put(conn, SHARE_USERNAMES_KEY, "1" if share else "0")


# --- what is sent -----------------------------------------------------------------------------


def build_context(
    conn: sqlite3.Connection, tournament: Tournament, view: matchup.MatchupView, share_names: bool
) -> PredictionContext:
    left, right, stage, result = view.left, view.right, view.stage, view.prediction
    if left is None or right is None or stage is None or result is None:
        raise CannotPredict(view.notice or CANNOT_PREDICT)
    model = matchup.load_model(conn, tournament, stage, [left.id, right.id])
    slot_of = {m.id: m.slot for m in view.pool}
    team_ids = {SIDE_A: left.id, SIDE_B: right.id}
    names = _usernames(conn, tournament.id, list(team_ids.values()))
    # Roster order (captain first, then by username), never osu! id order, which would tell the
    # provider who joined the platform first.
    ordered = {side: model.rosters[team_id] for side, team_id in team_ids.items()}
    labels = _player_labels(ordered[SIDE_A] + ordered[SIDE_B])
    data = {
        "stage": _stage_data(stage, effective_lineup_size(stage, tournament), share_names),
        "teams": {
            SIDE_A: left.name if share_names else "Team A",
            SIDE_B: right.name if share_names else "Team B",
        },
        "maps": [_map_data(c) for c in result.comparisons],
        "players": {
            side: [
                {
                    "name": names.get(osu_id, labels[osu_id]) if share_names else labels[osu_id],
                    "best_scores": {
                        pool_map.slot: model.best[(osu_id, pool_map.id)].overall.score
                        for pool_map in view.pool
                        if (osu_id, pool_map.id) in model.best
                    },
                }
                for osu_id in ordered[side]
            ]
            for side in team_ids
        },
        "stat_model": _stat_data(result, slot_of),
        "habits": {
            side: _habits_data(
                history.habits(conn, tournament.id, team_id, view.match.id, view.match.scheduled_at)
            )
            for side, team_id in team_ids.items()
        },
    }
    return PredictionContext(data, frozenset(slot_of.values()))


def _stage_data(stage: Stage, lineup_size: int, share_names: bool) -> dict:
    """The structured format fields. The format sentence is sent only when names may leave the
    server: it is free text for the reader, and the structured fields already say the same."""
    data = {
        "best_of": stage.best_of,
        "bans_per_team": stage.bans,
        "protects_per_team": stage.protects,
        "players_per_map": lineup_size,
        "is_qualifier": stage.is_qualifier,
    }
    if share_names:
        data["format"] = stage.format_text or "not set"
    return data


def _map_data(comparison: prediction.MapComparison) -> dict:
    pool_map: PoolMap = comparison.pool_map

    def expected(item: prediction.Expectation | None) -> dict | None:
        return None if item is None else {"score": round(item.score), "estimated": item.estimated}

    return {
        "slot": pool_map.slot,
        "bracket": pool_map.bracket,
        "required_mods": pool_map.required_mods,
        "freemod": pool_map.is_freemod,
        "tiebreaker": pool_map.is_tiebreaker,
        "stars": pool_map.star_rating,
        "expected_score": {SIDE_A: expected(comparison.side1), SIDE_B: expected(comparison.side2)},
        "stat_chance_A": None if comparison.prob is None else round(comparison.prob, 3),
    }


def _stat_data(result: prediction.MatchPrediction, slot_of: dict[int, str]) -> dict | None:
    series = result.series
    if series is None:
        return None
    side = {1: SIDE_A, 2: SIDE_B}
    maps_a, maps_b = series.expected_score
    return {
        "chance_A": round(series.win_prob, 3),
        "likely_score_A_B": [maps_a, maps_b],
        "orders": [
            {
                "first_pick": side[order.first_pick],
                "chance_A": round(order.win_prob, 3),
                "protects": {
                    side[s]: [slot_of[i] for i in ids] for s, ids in order.protects.items()
                },
                "bans": {side[s]: [slot_of[i] for i in ids] for s, ids in order.bans.items()},
                "picks": [{"team": side[s], "slot": slot_of[i]} for s, i in order.picks],
            }
            for order in series.orders
        ],
    }


def _habits_data(habits: Habits | None) -> dict | None:
    if habits is None:
        return None
    return {
        "pick_share_by_bracket": {k: round(v, 2) for k, v in habits.pick_rate.items()},
        "ban_share_by_bracket": {k: round(v, 2) for k, v in habits.ban_rate.items()},
        "first_pick_rate": None
        if habits.first_pick_rate is None
        else round(habits.first_pick_rate, 2),
    }


def _usernames(conn: sqlite3.Connection, tournament_id: int, team_ids: list[int]) -> dict[int, str]:
    """Known usernames of both teams' players and the roster, by osu! id."""
    names = {m.osu_id: m.username for m in members_repo.list_for_tournament(conn, tournament_id)}
    for team_id in team_ids:
        names.update({p.osu_id: p.username for p in teams_repo.list_players(conn, team_id)})
    return {osu_id: name for osu_id, name in names.items() if name}


def _player_labels(osu_ids: list[int]) -> dict[int, str]:
    """ "Player A", "Player B", ... in the order given; "Player 27" and up after the alphabet."""
    letters = string.ascii_uppercase
    return {
        osu_id: f"Player {letters[n] if n < len(letters) else n + 1}"
        for n, osu_id in enumerate(osu_ids)
    }


# --- what is kept -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """A match that can be sent: the page's view of it and the data the provider gets."""

    view: matchup.MatchupView
    context: PredictionContext


def prepare(conn: sqlite3.Connection, tournament: Tournament, match: Match) -> Prepared:
    """Build what would be sent. Raises `CannotPredict` when there is nothing to send, so the caller
    can refuse before it spends anything on the ask."""
    view = matchup.build_matchup(conn, tournament, match)
    return Prepared(view, build_context(conn, tournament, view, share_usernames(conn)))


async def estimate(
    conn: sqlite3.Connection,
    provider: AiProvider,
    prepared: Prepared,
    model_version: str,
    now: datetime,
) -> None:
    """Ask the provider for an estimate of the match and store it. Nothing is stored when the
    provider fails (`AiError`). No transaction is open while the provider is awaited."""
    answer = await provider.predict(prepared.context)
    with transaction(conn):
        store_estimate(conn, prepared.view, answer, model_version, now)


def store_estimate(
    conn: sqlite3.Connection,
    view: matchup.MatchupView,
    answer: AiPrediction,
    model_version: str,
    now: datetime,
) -> None:
    """Keep the answer as a `kind='ai'` snapshot. Its payload is read back by `latest_estimate`;
    teams are kept by id so it does not depend on which side the page shows on the left."""
    left, right = view.left, view.right
    if left is None or right is None:
        raise CannotPredict(CANNOT_PREDICT)
    team_id = {SIDE_A: left.id, SIDE_B: right.id}
    chance_left = (
        answer.win_percent / 100 if answer.winner == SIDE_A else 1 - answer.win_percent / 100
    )
    payload = {
        "winner_team_id": team_id[answer.winner],
        "win_percent": answer.win_percent,
        "bans": {str(team_id[side]): list(slots) for side, slots in answer.bans.items()},
        "picks": [{"team_id": team_id[side], "slot": slot} for side, slot in answer.picks],
        "reasoning": answer.reasoning,
    }
    predictions_repo.insert_snapshot(
        conn,
        view.match.id,
        KIND,
        to_utc_iso(now),
        chance_left if left.id == view.match.team1_id else 1 - chance_left,
        None,
        json.dumps(payload, separators=(",", ":")),
        model_version,
    )


def latest_estimate(
    conn: sqlite3.Connection, match: Match, left: Team | None, right: Team | None
) -> AiEstimate | None:
    """The newest stored AI estimate of the match, in the names of the page's two teams."""
    snapshot = predictions_repo.latest_snapshot(conn, match.id, KIND)
    if snapshot is None or left is None or right is None:
        return None
    payload = json.loads(snapshot.payload)
    name = {left.id: left.name, right.id: right.name}

    def team_name(team_id: int) -> str:
        return name.get(team_id, UNKNOWN_TEAM)

    return AiEstimate(
        created_at=snapshot.created_at,
        model=snapshot.model_version,
        left_chance=(
            snapshot.win_prob_team1 if left.id == match.team1_id else 1 - snapshot.win_prob_team1
        ),
        winner_name=team_name(payload["winner_team_id"]),
        win_percent=payload["win_percent"],
        bans=[(team_name(int(tid)), slots) for tid, slots in payload["bans"].items()],
        picks=[(team_name(pick["team_id"]), pick["slot"]) for pick in payload["picks"]],
        reasoning=payload["reasoning"],
    )
