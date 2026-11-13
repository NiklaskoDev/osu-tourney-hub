"""Games and scores from the events of an osu! multiplayer lobby. Pure functions.

Ported from `collect()` of the reference script: an event with a `game` is one played map, its
`scores` hold one entry per player, accuracy is 0 to 1 and the miss count sits in `statistics`
under `count_miss` (older shape) or `miss` (lazer). Unlike the script this keeps only finished
games and lets the caller decide what counts as a hit.
"""

from collections.abc import Iterable
from typing import Any

from app.clients.osu import api_mods
from app.clock import utc_iso_or_none
from app.domain.models import MpGame, MpScore
from app.domain.modes import API_MODE_NAMES

MAX_SCORE = 2_000_000_000  # what the scores table accepts
# The room's scoring type as the API names it -> the scoring stored with a score.
SCORING_TYPES = {"scorev2": "v2", "score": "v1"}
UNKNOWN_SCORING = "unknown"  # accuracy and combo rooms rank by something else
_MODE_BY_NAME = {name: code for code, name in API_MODE_NAMES.items()}


def parse_games(events: Iterable[dict[str, Any]]) -> list[MpGame]:
    """Every finished game with a usable beatmap and mode. A game still being played has no end
    time and no scores yet; it is read again once it is over."""
    games = []
    for event in events:
        game = event.get("game")
        if isinstance(game, dict) and game.get("end_time"):
            parsed = _parse_game(game)
            if parsed is not None:
                games.append(parsed)
    return games


def last_final_event_id(events: list[dict[str, Any]]) -> int | None:
    """The newest event id from which nothing before or at it can still change: events up to the
    first game that has not ended yet, or all of them when every game has ended. A later read
    starts after this id, so a game in progress is read again once it is over. None when the very
    first event is still in progress or there are no events."""
    last: int | None = None
    for event in sorted(events, key=lambda item: item["id"]):
        game = event.get("game")
        if isinstance(game, dict) and not game.get("end_time"):
            break
        last = event["id"]
    return last


def _parse_game(game: dict[str, Any]) -> MpGame | None:
    game_id = _positive_int(game.get("id"))
    beatmap_id = _positive_int(game.get("beatmap_id"))
    gamemode = _mode_of(game)
    if game_id is None or beatmap_id is None or gamemode is None:
        return None
    room_mods = api_mods(game.get("mods"), gamemode)
    scores = [
        parsed
        for raw in game.get("scores") or []
        if (parsed := _parse_score(raw, gamemode)) is not None
    ]
    return MpGame(
        game_id=game_id,
        beatmap_id=beatmap_id,
        gamemode=gamemode,
        scoring=SCORING_TYPES.get(str(game.get("scoring_type")), UNKNOWN_SCORING),
        mods=room_mods or "",
        played_at=utc_iso_or_none(game.get("end_time")) or utc_iso_or_none(game.get("start_time")),
        scores=scores,
    )


def _parse_score(raw: object, gamemode: int) -> MpScore | None:
    if not isinstance(raw, dict):
        return None
    osu_id = _positive_int(raw.get("user_id"))
    score = raw.get("score")
    accuracy = raw.get("accuracy")
    mods = api_mods(raw.get("mods"), gamemode)
    valid = (
        osu_id is not None
        and _is_number(score)
        and 0 <= score <= MAX_SCORE
        and _is_number(accuracy)
        and 0 <= accuracy <= 1
        and mods is not None
    )
    if not valid:
        return None
    statistics = raw.get("statistics")
    stats = statistics if isinstance(statistics, dict) else {}
    match = raw.get("match")
    passed = match.get("pass") if isinstance(match, dict) and "pass" in match else raw.get("passed")
    return MpScore(
        osu_id=osu_id,
        score=int(score),
        accuracy=round(accuracy * 100, 4),
        max_combo=_count(raw.get("max_combo")),
        misses=_count(stats.get("count_miss", stats.get("miss"))),
        mods=mods,
        passed=True if passed is None else bool(passed),
    )


def _mode_of(game: dict[str, Any]) -> int | None:
    mode = game.get("mode_int")
    if isinstance(mode, int) and not isinstance(mode, bool) and mode in API_MODE_NAMES:
        return mode
    return _MODE_BY_NAME.get(str(game.get("mode")))


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
