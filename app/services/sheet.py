"""The mappool sheet's rules: which scores count on a map, the best one per player, averages.

Pure functions. A score counts when it was passed and uses the tournament's scoring type (D3), so
ScoreV1 never mixes into ScoreV2 averages. One exception (plan 002): osu! offers no ScoreV2 figure,
so lazer standardised scores count on ScoreV2 tournaments as a stand-in, marked "lazer" on the
page. Scores older than `OLD_SCORE_DAYS` still count; they are only marked old (D6).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.clock import from_utc_iso
from app.domain.models import PoolMap, Score

OLD_SCORE_DAYS = 182
DOUBLE_TIME_ALIASES = {"NC": "DT"}  # Nightcore is Double Time with a different sound

BestKey = tuple[int, int]  # (osu_id, pool_map_id)


@dataclass(frozen=True)
class BestScore:
    """A player's best on one map: the overall best and the best per mod combination (a freemod
    map keeps one per combination; every other map has just the one)."""

    overall: Score
    by_mods: dict[str, Score]


@dataclass(frozen=True)
class Average:
    """Mean of the best scores over the maps that have one, and how many maps that is."""

    mean_score: float | None
    mean_accuracy: float | None
    covered: int
    total: int


def is_stand_in(score: Score, scoring: str) -> bool:
    """A lazer standardised score counted on a ScoreV2 tournament, close to ScoreV2 but not it."""
    return scoring == "v2" and score.scoring == "lazer"


def counts_for(score: Score, scoring: str) -> bool:
    return score.passed and (score.scoring == scoring or is_stand_in(score, scoring))


def uncounted_reason(pool_map: PoolMap, score: Score, scoring: str) -> str | None:
    """Why a score on this map is left out of the sheet, or None when it counts."""
    if not score.passed:
        return "Not passed"
    if score.scoring != scoring and not is_stand_in(score, scoring):
        return "Other scoring type"
    if not accepts_mods(pool_map, score.mods):
        return "Mods do not match the map"
    return None


def is_old(score: Score, now: datetime) -> bool:
    try:
        return now - from_utc_iso(score.played_at) > timedelta(days=OLD_SCORE_DAYS)
    except ValueError:
        return False


def rank_key(score: Score) -> tuple[int, float, str, int]:
    """Larger is better: score, then accuracy, then the more recent one, then the later row."""
    return (score.score, score.accuracy, score.played_at, score.id)


def accepts_mods(pool_map: PoolMap, mods: str) -> bool:
    """Freemod and tiebreaker maps take any mods; every other map only its required mods."""
    if pool_map.is_freemod or pool_map.is_tiebreaker:
        return True
    return _as_double_time(mods) == _as_double_time(pool_map.required_mods)


def best_scores(
    pool: Iterable[PoolMap], scores: Iterable[Score], scoring: str
) -> dict[BestKey, BestScore]:
    """The best counting score of every player on every pool map. Players without one are absent."""
    by_beatmap: dict[int, list[PoolMap]] = {}
    for pool_map in pool:
        if pool_map.beatmap_id is not None:
            by_beatmap.setdefault(pool_map.beatmap_id, []).append(pool_map)
    found: dict[BestKey, dict[str, Score]] = {}
    for score in scores:
        if not counts_for(score, scoring):
            continue
        for pool_map in by_beatmap.get(score.beatmap_id, []):
            if not accepts_mods(pool_map, score.mods):
                continue
            per_mods = found.setdefault((score.osu_id, pool_map.id), {})
            kept = per_mods.get(score.mods)
            if kept is None or rank_key(score) > rank_key(kept):
                per_mods[score.mods] = score
    return {
        key: BestScore(max(per_mods.values(), key=rank_key), per_mods)
        for key, per_mods in found.items()
    }


def personal_average(
    pool: Iterable[PoolMap], best: dict[BestKey, BestScore], osu_id: int
) -> Average:
    """One player's mean over `pool` (all maps, or one bracket's)."""
    maps = list(pool)
    chosen = [best[(osu_id, m.id)].overall for m in maps if (osu_id, m.id) in best]
    return _average(chosen, len(maps))


def map_average(
    pool_map: PoolMap, best: dict[BestKey, BestScore], osu_ids: Iterable[int]
) -> tuple[float | None, float | None, int]:
    """Mean score, mean accuracy and how many players have a score on one map."""
    chosen = [best[(o, pool_map.id)].overall for o in osu_ids if (o, pool_map.id) in best]
    if not chosen:
        return None, None, 0
    return _mean(s.score for s in chosen), _mean(s.accuracy for s in chosen), len(chosen)


def team_average(
    pool: Iterable[PoolMap], best: dict[BestKey, BestScore], osu_ids: Iterable[int]
) -> Average:
    """Mean over the maps that anyone has played of that map's mean score across the team."""
    players = list(osu_ids)
    maps = list(pool)
    per_map = [map_average(m, best, players) for m in maps]
    played = [(score, accuracy) for score, accuracy, count in per_map if count]
    if not played:
        return Average(None, None, 0, len(maps))
    return Average(
        _mean(score for score, _ in played),
        _mean(accuracy for _, accuracy in played),
        len(played),
        len(maps),
    )


def _average(chosen: list[Score], total: int) -> Average:
    if not chosen:
        return Average(None, None, 0, total)
    return Average(
        _mean(s.score for s in chosen), _mean(s.accuracy for s in chosen), len(chosen), total
    )


def _mean(values: Iterable[float]) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers)


def _as_double_time(mods: str) -> tuple[str, ...]:
    """Mod acronyms of a canonical string with Nightcore read as Double Time."""
    acronyms = [mods[i : i + 2] for i in range(0, len(mods), 2)]
    return tuple(sorted(DOUBLE_TIME_ALIASES.get(a, a) for a in acronyms))
