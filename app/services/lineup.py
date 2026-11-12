"""Lineup suggestion: for every map, the players with the best scores. Pure functions.

Guests never play. Substitutes are left out unless asked for. On a freemod map the tournament's
freemod rule decides which scores may be used.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from app.domain.models import Member, PoolMap, Score
from app.services.sheet import BestKey, BestScore, rank_key


@dataclass(frozen=True)
class Pick:
    osu_id: int
    score: Score

    @property
    def mods(self) -> str:
        return self.score.mods


@dataclass(frozen=True)
class MapSuggestion:
    picks: list[Pick]
    incomplete: bool  # fewer players with a usable score than the lineup size
    rule_unmet: bool  # a freemod rule that could not be satisfied with the scores there are


def eligible_players(
    members: Iterable[Member],
    include_substitutes: bool = False,
    available_ids: Collection[int] | None = None,
) -> list[int]:
    """Who may be suggested: not guests, substitutes only when asked for, and, when
    `available_ids` is given, only people in it."""
    return [
        member.osu_id
        for member in members
        if member.role != "guest"
        and (include_substitutes or not member.is_substitute)
        and (available_ids is None or member.osu_id in available_ids)
    ]


def suggest(
    pool: Iterable[PoolMap],
    best: dict[BestKey, BestScore],
    players: Collection[int],
    lineup_size: int,
    freemod_rule: str,
) -> dict[int, MapSuggestion]:
    """The top `lineup_size` players per map by score (ties: accuracy, then the more recent
    score), by pool map id."""
    return {
        pool_map.id: _suggest_map(pool_map, best, players, lineup_size, freemod_rule)
        for pool_map in pool
    }


def _suggest_map(
    pool_map: PoolMap,
    best: dict[BestKey, BestScore],
    players: Collection[int],
    size: int,
    freemod_rule: str,
) -> MapSuggestion:
    options: dict[int, list[Score]] = {}
    for osu_id in players:
        found = best.get((osu_id, pool_map.id))
        usable = _usable_scores(pool_map, found, freemod_rule) if found else []
        if usable:
            options[osu_id] = usable
    ranked = sorted(options, key=lambda osu_id: rank_key(options[osu_id][0]), reverse=True)
    picks = [Pick(osu_id, options[osu_id][0]) for osu_id in ranked[:size]]
    rule_unmet = False
    if pool_map.is_freemod and freemod_rule == "two_distinct_mods" and len(picks) >= 2:
        if len({pick.mods for pick in picks}) < 2:
            swapped = _swap_for_two_mods(picks, options)
            rule_unmet = swapped is None
            picks = swapped or picks
    return MapSuggestion(picks, len(picks) < size, rule_unmet)


def _usable_scores(pool_map: PoolMap, found: BestScore, freemod_rule: str) -> list[Score]:
    """A player's scores on the map that the rules allow, best first."""
    if not pool_map.is_freemod:
        return [found.overall]
    scores = list(found.by_mods.values())
    if freemod_rule == "each_player_mod":
        scores = [score for score in scores if score.mods]
    return sorted(scores, key=rank_key, reverse=True)


def _swap_for_two_mods(picks: list[Pick], options: dict[int, list[Score]]) -> list[Pick] | None:
    """All picks use the same mods: change the one pick that costs the least score so that a
    second mod combination appears. None when nobody has a score with another combination."""
    shared = picks[0].mods
    swaps = []
    for index, current in enumerate(picks):
        taken = {pick.osu_id for position, pick in enumerate(picks) if position != index}
        for osu_id, scores in options.items():
            if osu_id in taken:
                continue
            for score in scores:
                if score.mods != shared:
                    change = score.score - current.score.score
                    swaps.append(((change, rank_key(score)), index, Pick(osu_id, score)))
    if not swaps:
        return None
    _, index, replacement = max(swaps, key=lambda swap: swap[0])
    return [replacement if position == index else pick for position, pick in enumerate(picks)]
