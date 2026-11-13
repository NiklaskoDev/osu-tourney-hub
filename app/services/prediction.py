"""The statistical prediction model. Pure functions, no I/O.

A team's expected score on a map is the sum of its best players' best scores. Two expected scores
become a chance to win the map, and a pick/ban simulation turns the map chances into a chance to
win the series. Everything here is an estimate from the scores there are: a missing score is
filled in from the player's other maps and marked "estimated", and a team without enough scores on
a map has "no data" there.

Side 1 and side 2 are the two teams of a simulation; the callers decide which team is which.
"""

import math
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from app.domain.models import PoolMap, Stage
from app.services.sheet import BestKey, BestScore

MODEL_VERSION = "stat-2"  # stat-2: the teams' pick and ban habits bend the simulation
# Steepness of the map curve: a team 8 % ahead of the mean score wins a map about 73 % of the time.
K = 0.08
HABIT_WEIGHT = 0.3  # how much a team's own pick and ban habits bend the simulated choices
MIN_SEED_COVERAGE = 0.5  # a team needs scores on this share of a qualifier pool to be ranked

SIDES = (1, 2)
ACTUAL, ESTIMATED, NO_DATA = "actual", "estimated", "no data"

FinalScore = tuple[int, int]  # maps won by (side 1, side 2)


@dataclass(frozen=True)
class Expectation:
    """What a team is expected to score on a map; `estimated` when a missing player's score was
    filled in from their other maps."""

    score: float
    estimated: bool


@dataclass(frozen=True)
class Habits:
    """How often a team picked and banned maps of a bracket in earlier rounds (0 to 1)."""

    pick_rate: Mapping[str, float]
    ban_rate: Mapping[str, float]
    first_pick_rate: float | None = None  # shown on the opponent page; the simulation tries both


@dataclass(frozen=True)
class SeriesMap:
    """A map of the pool as the simulation sees it: `prob` is side 1's chance to win it (None =
    no data, treated as a coin flip)."""

    pool_map_id: int
    bracket: str
    prob: float | None
    is_tiebreaker: bool


@dataclass(frozen=True)
class OrderResult:
    """The simulation for one first-pick order."""

    first_pick: int  # the side that picks first
    win_prob: float  # of side 1
    protects: dict[int, list[int]]  # side -> pool map ids
    bans: dict[int, list[int]]
    picks: list[tuple[int, int]]  # (side, pool map id) in the order they are picked


@dataclass(frozen=True)
class SeriesResult:
    """The series chance averaged over the first-pick orders that were simulated."""

    win_prob: float  # of side 1
    score_probs: dict[FinalScore, float]
    orders: list[OrderResult]

    @property
    def expected_score(self) -> FinalScore:
        """The most likely final score among those the likelier winner wins."""
        side1_favoured = self.win_prob >= 0.5
        candidates = [seat for seat in self.score_probs if (seat[0] > seat[1]) == side1_favoured]
        return max(candidates, key=lambda seat: (round(self.score_probs[seat], 9), seat))


@dataclass(frozen=True)
class SeedEstimate:
    seed: int | None  # our rank among the ranked teams; None when we have too few scores
    ranked: int  # teams with enough scores to be ranked
    left_out: int  # teams with scores on too few maps


@dataclass(frozen=True)
class MapComparison:
    pool_map: PoolMap
    side1: Expectation | None
    side2: Expectation | None
    prob: float | None  # side 1 wins the map

    @property
    def quality(self) -> str:
        if self.side1 is None or self.side2 is None:
            return NO_DATA
        return ESTIMATED if self.side1.estimated or self.side2.estimated else ACTUAL


@dataclass(frozen=True)
class MatchPrediction:
    comparisons: list[MapComparison]
    series: SeriesResult | None  # None when the stage has no usable format
    with_data: int  # maps with an expectation for both teams
    total: int  # maps of the pool


@dataclass(frozen=True)
class SeriesFormat:
    best_of: int
    bans: int  # per team
    protects: int  # per team


# --- single maps -----------------------------------------------------------------------------


def map_win_prob(side1: float | None, side2: float | None) -> float | None:
    """Side 1's chance to win a map from both expected scores: a logistic curve on the lead
    relative to the mean. None when either side has no data."""
    if side1 is None or side2 is None:
        return None
    mean = (side1 + side2) / 2
    if mean <= 0:
        return None
    return 1 / (1 + math.exp(-(side1 - side2) / (K * mean)))


def player_ratios(
    pool: Iterable[PoolMap], best: Mapping[BestKey, BestScore], osu_ids: Collection[int]
) -> dict[int, float]:
    """Each player's typical score relative to their team's mean on the same map: the median
    over the maps where they have a score. Players without any score are absent."""
    ratios: dict[int, list[float]] = defaultdict(list)
    for pool_map in pool:
        scores = {
            osu_id: best[(osu_id, pool_map.id)].overall.score
            for osu_id in osu_ids
            if (osu_id, pool_map.id) in best
        }
        mean = sum(scores.values()) / len(scores) if scores else 0
        if mean <= 0:
            continue
        for osu_id, score in scores.items():
            ratios[osu_id].append(score / mean)
    return {osu_id: median(values) for osu_id, values in ratios.items()}


def expected_team_score(
    pool_map: PoolMap,
    best: Mapping[BestKey, BestScore],
    osu_ids: Collection[int],
    size: int,
    ratios: Mapping[int, float],
) -> Expectation | None:
    """The sum of the `size` best scores of the team's players on this map. A player without a
    score counts with their usual ratio to the teammates who have one (marked estimated). None
    when fewer than `size` players have a score or an estimate."""
    actual = {
        osu_id: best[(osu_id, pool_map.id)].overall.score
        for osu_id in osu_ids
        if (osu_id, pool_map.id) in best
    }
    candidates = [(float(score), False) for score in actual.values()]
    if actual:
        mean = sum(actual.values()) / len(actual)
        candidates += [
            (ratios[osu_id] * mean, True)
            for osu_id in osu_ids
            if osu_id not in actual and osu_id in ratios
        ]
    chosen = sorted(candidates, key=lambda candidate: candidate[0], reverse=True)[:size]
    if size < 1 or len(chosen) < size:
        return None
    return Expectation(sum(score for score, _ in chosen), any(estimated for _, estimated in chosen))


def team_expectations(
    pool: Sequence[PoolMap],
    best: Mapping[BestKey, BestScore],
    osu_ids: Collection[int],
    size: int,
) -> dict[int, Expectation | None]:
    """The team's expected score on every pool map, by pool map id."""
    ratios = player_ratios(pool, best, osu_ids)
    return {m.id: expected_team_score(m, best, osu_ids, size, ratios) for m in pool}


# --- the series ------------------------------------------------------------------------------


def series_format(stage: Stage) -> SeriesFormat | None:
    """The stage's format, or None when it has no best-of or an even one (no clear winner)."""
    if not stage.best_of or stage.best_of < 1 or stage.best_of % 2 == 0:
        return None
    return SeriesFormat(stage.best_of, stage.bans or 0, stage.protects or 0)


def simulate_series(
    maps: Sequence[SeriesMap],
    best_of: int,
    bans: int,
    protects: int,
    first_pick: int | None = None,
    habits: tuple[Habits | None, Habits | None] = (None, None),
) -> SeriesResult:
    """Simulate protects, bans and picks, then play the series out exactly.

    Each side protects the maps it is strongest on (the other side cannot ban those), the sides
    ban alternately the unprotected map the other side is strongest on, then they pick alternately
    their own strongest remaining map. The tiebreaker is played at one win short of the finish.
    Habits (side 1's, side 2's) bend these choices by `HABIT_WEIGHT`. The chance to win is the sum
    over all ways the series can go. With `first_pick` None both first-pick orders are simulated
    and averaged.
    """
    if best_of < 1 or best_of % 2 == 0:
        raise ValueError("best_of must be an odd number")
    orders = [
        _simulate_order(maps, best_of, bans, protects, side, habits) for side in _firsts(first_pick)
    ]
    scores: dict[FinalScore, float] = defaultdict(float)
    for _, distribution in orders:
        for seat, probability in distribution.items():
            scores[seat] += probability / len(orders)
    return SeriesResult(
        sum(order.win_prob for order, _ in orders) / len(orders),
        dict(scores),
        [order for order, _ in orders],
    )


def _firsts(first_pick: int | None) -> tuple[int, ...]:
    return SIDES if first_pick is None else (first_pick,)


def _advantage(side: int, prob: float | None) -> float:
    """The chance that `side` wins the map; a map without data is a coin flip."""
    chance = 0.5 if prob is None else prob
    return chance if side == 1 else 1 - chance


def _simulate_order(
    maps: Sequence[SeriesMap],
    best_of: int,
    bans: int,
    protects: int,
    first: int,
    habits: tuple[Habits | None, Habits | None],
) -> tuple[OrderResult, dict[FinalScore, float]]:
    pool = [m for m in maps if not m.is_tiebreaker]
    tiebreaker = next((m for m in maps if m.is_tiebreaker), None)
    sides = (first, 3 - first)
    protected = _protect(pool, protects, sides)
    banned = _ban(pool, bans, sides, protected, habits)
    picks = _pick(pool, best_of - 1, sides, banned, habits)
    by_id = {m.pool_map_id: m for m in pool}
    sequence = [_advantage(1, by_id[map_id].prob) for _, map_id in picks]
    tiebreak = _advantage(1, tiebreaker.prob) if tiebreaker else 0.5
    distribution = _play(sequence, tiebreak, best_of)
    wins = sum(p for (won, _), p in distribution.items() if won > best_of // 2)
    return (
        OrderResult(first, wins, protected.by_side, banned.by_side, picks),
        distribution,
    )


class _Chosen:
    """Maps taken out of play so far, by the side that took them."""

    def __init__(self) -> None:
        self.by_side = {1: [], 2: []}

    def all(self) -> set[int]:
        return {map_id for ids in self.by_side.values() for map_id in ids}


def _protect(pool: Sequence[SeriesMap], count: int, sides: tuple[int, int]) -> _Chosen:
    protected = _Chosen()
    for _ in range(count):
        for side in sides:
            options = [m for m in pool if m.pool_map_id not in protected.all()]
            if options:
                best = max(options, key=lambda m: _advantage(side, m.prob))
                protected.by_side[side].append(best.pool_map_id)
    return protected


def _ban(
    pool: Sequence[SeriesMap],
    count: int,
    sides: tuple[int, int],
    protected: _Chosen,
    habits: tuple[Habits | None, Habits | None],
) -> _Chosen:
    banned = _Chosen()
    for _ in range(count):
        for side in sides:
            out = protected.all() | banned.all()
            options = [m for m in pool if m.pool_map_id not in out]
            if not options:
                continue
            rate = _rate(habits[side - 1], "ban_rate")
            best = max(
                options,
                key=lambda m: _advantage(3 - side, m.prob) + HABIT_WEIGHT * rate(m.bracket),
            )
            banned.by_side[side].append(best.pool_map_id)
    return banned


def _pick(
    pool: Sequence[SeriesMap],
    count: int,
    sides: tuple[int, int],
    banned: _Chosen,
    habits: tuple[Habits | None, Habits | None],
) -> list[tuple[int, int]]:
    remaining = [m for m in pool if m.pool_map_id not in banned.all()]
    picks: list[tuple[int, int]] = []
    for turn in range(count):
        if not remaining:
            break
        side = sides[turn % 2]
        rate = _rate(habits[side - 1], "pick_rate")
        best = max(
            remaining, key=lambda m: _advantage(side, m.prob) + HABIT_WEIGHT * rate(m.bracket)
        )
        picks.append((side, best.pool_map_id))
        remaining.remove(best)
    return picks


def _rate(habits: Habits | None, which: str) -> Callable[[str], float]:
    """A function from bracket to the team's habit rate (0 without history)."""
    table = getattr(habits, which, None) or {}
    return lambda bracket: table.get(bracket, 0.0)


def _play(sequence: Sequence[float], tiebreak: float, best_of: int) -> dict[FinalScore, float]:
    """The chance of every final score, walking the series game by game. `sequence[i]` is side
    1's chance on the i-th game (a coin flip once the picks run out); the deciding game at one win
    short of the finish uses `tiebreak`."""
    needed = best_of // 2 + 1
    states: dict[FinalScore, float] = {(0, 0): 1.0}
    final: dict[FinalScore, float] = defaultdict(float)
    for game in range(best_of):
        following: dict[FinalScore, float] = defaultdict(float)
        for (one, two), chance in states.items():
            if one == two == needed - 1:
                win = tiebreak
            else:
                win = sequence[game] if game < len(sequence) else 0.5
            for seat, weight in (((one + 1, two), win), ((one, two + 1), 1 - win)):
                target = final if needed in seat else following
                target[seat] += chance * weight
        states = following
    return dict(final)


# --- whole matches, seeds ----------------------------------------------------------------------


def compare_maps(
    pool: Sequence[PoolMap],
    side1: Mapping[int, Expectation | None],
    side2: Mapping[int, Expectation | None],
) -> list[MapComparison]:
    comparisons = []
    for pool_map in pool:
        one, two = side1.get(pool_map.id), side2.get(pool_map.id)
        prob = map_win_prob(one.score if one else None, two.score if two else None)
        comparisons.append(MapComparison(pool_map, one, two, prob))
    return comparisons


def predict_match(
    pool: Sequence[PoolMap],
    stage: Stage,
    side1: Mapping[int, Expectation | None],
    side2: Mapping[int, Expectation | None],
    habits: tuple[Habits | None, Habits | None] = (None, None),
) -> MatchPrediction:
    """Map by map comparison and, when the stage has a format, the simulated series."""
    comparisons = compare_maps(pool, side1, side2)
    stage_format = series_format(stage)
    series = None
    if stage_format is not None:
        maps = [
            SeriesMap(c.pool_map.id, c.pool_map.bracket, c.prob, c.pool_map.is_tiebreaker)
            for c in comparisons
        ]
        series = simulate_series(
            maps, stage_format.best_of, stage_format.bans, stage_format.protects, None, habits
        )
    with_data = sum(1 for c in comparisons if c.prob is not None)
    return MatchPrediction(comparisons, series, with_data, len(comparisons))


def qualifier_seed(
    team_id: int,
    expectations: Mapping[int, Mapping[int, Expectation | None]],
    map_ids: Sequence[int],
) -> SeedEstimate:
    """Our rank among the teams by the sum over the maps of expected score relative to the map's
    median (so a hard map does not count more than an easy one). Teams with scores on fewer than
    `MIN_SEED_COVERAGE` of the maps are left out and counted."""
    total = len(map_ids)
    covered = {
        team: {m: e for m in map_ids if (e := per_map.get(m)) is not None}
        for team, per_map in expectations.items()
    }
    ranked = {
        team: found
        for team, found in covered.items()
        if total and len(found) >= MIN_SEED_COVERAGE * total
    }
    left_out = len(expectations) - len(ranked)
    medians = {
        map_id: median(found[map_id].score for found in ranked.values() if map_id in found)
        for map_id in map_ids
        if any(map_id in found for found in ranked.values())
    }
    totals = {
        team: sum(found[m].score / medians[m] for m in found if medians.get(m))
        for team, found in ranked.items()
    }
    if team_id not in totals:
        return SeedEstimate(None, len(ranked), left_out)
    ahead = sum(1 for value in totals.values() if value > totals[team_id])
    return SeedEstimate(ahead + 1, len(ranked), left_out)
