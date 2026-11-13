"""What the live page shows about a match under way: the running score, the maps played, what was
banned and protected, the map being played and the model's suggestion for the next pick.

Built from the stored match events (the live follower keeps them up to date), so the same state
can be rebuilt for a page opened later. The "left" team is ours when we play in the match, as on
the match page; the simulation's side 1 is the left team.
"""

import sqlite3
from dataclasses import dataclass, field

from app.domain.models import Match, PoolMap, Tournament
from app.repos import match_events as events_repo
from app.repos import stages as stages_repo
from app.repos.match_events import MatchEvent
from app.services import history, matchup, prediction
from app.services.prediction import SeriesFormat, SeriesMap
from app.services.schedule import UNKNOWN_TEAM

WAITING, RUNNING, FINISHED, STOPPED = "waiting", "running", "finished", "stopped"
STATUS_LABELS = {
    WAITING: "Waiting for the lobby",
    RUNNING: "Following the lobby",
    FINISHED: "The lobby is closed",
    STOPPED: "Live updates are stopped",
}
UNKNOWN_MAP = "?"
NOTICE_PLAYING = "A map is being played. The pick suggestion comes back when it ends."


@dataclass(frozen=True)
class MapRef:
    """A map as the page names it: its slot and a title (or a placeholder)."""

    slot: str
    title: str


@dataclass(frozen=True)
class PlayedMap:
    map: MapRef
    winner: str | None
    left_score: int | None
    right_score: int | None


@dataclass(frozen=True)
class RemovedMap:
    """A map that was protected or banned; `team` is None when nobody could be named."""

    kind: str
    map: MapRef
    team: str | None


@dataclass(frozen=True)
class NextPick:
    """The model's suggestion. `team` is None for the tiebreaker (nobody picks it);
    `picker_known` is False when the lobby does not tell whose turn it is, and the suggestion is
    then for the left team."""

    team: str | None
    map: MapRef
    team_chance: float | None  # that the picking team wins the map; None without data
    decider: bool
    picker_known: bool


@dataclass(frozen=True)
class LiveState:
    match_id: int
    status: str
    notice: str | None  # a problem to show, e.g. osu! not answering
    title: str
    left_name: str
    right_name: str
    left_wins: int = 0
    right_wins: int = 0
    best_of: int | None = None
    decided: bool = False
    played: list[PlayedMap] = field(default_factory=list)
    removed: list[RemovedMap] = field(default_factory=list)
    current: MapRef | None = None  # the map being played right now
    open_maps: list[MapRef] = field(default_factory=list)
    next_pick: NextPick | None = None
    left_win_prob: float | None = None  # series chance of the left team from here
    prediction_notice: str | None = None  # why there is no suggestion

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def first_to(self) -> int | None:
        return None if self.best_of is None else self.best_of // 2 + 1

    @property
    def is_final(self) -> bool:
        """Nothing more will come: the stream of the page ends after this state."""
        return self.status == FINISHED


def build_state(
    conn: sqlite3.Connection,
    tournament: Tournament,
    match: Match,
    status: str,
    notice: str | None = None,
    in_progress_beatmap_id: int | None = None,
) -> LiveState:
    view = matchup.build_matchup(conn, tournament, match)
    pool = stages_repo.list_maps(conn, match.stage_id) if match.stage_id is not None else []
    by_id = {m.id: m for m in pool}
    left_side = 2 if view.left is not None and view.left.id == match.team2_id else 1
    left_name = view.left.name if view.left else UNKNOWN_TEAM
    right_name = view.right.name if view.right else UNKNOWN_TEAM

    def team_of(side: int | None) -> str | None:
        if side is None:
            return None
        return left_name if side == left_side else right_name

    events = events_repo.list_for_match(conn, match.id)
    games = [e for e in events if e.kind in ("pick", "tiebreaker")]
    left_wins = sum(1 for e in games if e.winner_side == left_side)
    right_wins = sum(1 for e in games if e.winner_side == 3 - left_side)
    fmt = prediction.series_format(view.stage) if view.stage else None
    decided = fmt is not None and max(left_wins, right_wins) >= fmt.best_of // 2 + 1
    taken = {e.pool_map_id for e in events if e.kind in ("ban", "pick", "tiebreaker")}
    current = _current(in_progress_beatmap_id, pool)
    next_pick, win_prob = None, None
    if not decided and current is None:
        next_pick, win_prob = _suggest(
            conn, tournament, match, view, fmt, events, (left_side, left_wins, right_wins), taken
        )
    return LiveState(
        match_id=match.id,
        status=status,
        notice=notice,
        title=view.title,
        left_name=left_name,
        right_name=right_name,
        left_wins=left_wins,
        right_wins=right_wins,
        best_of=fmt.best_of if fmt else None,
        decided=decided,
        played=[_played(e, by_id, left_side, team_of) for e in games],
        removed=[
            RemovedMap(e.kind, _ref(e, by_id), team_of(e.team_side))
            for e in events
            if e.kind in ("protect", "ban")
        ],
        current=current,
        open_maps=[_pool_ref(m) for m in pool if m.id not in taken],
        next_pick=next_pick,
        left_win_prob=win_prob,
        prediction_notice=None if next_pick or decided else _why_none(view, fmt, current),
    )


def _label(pool_map: PoolMap) -> str:
    if not pool_map.title:
        return "Map not announced"
    version = f" [{pool_map.version}]" if pool_map.version else ""
    return f"{pool_map.artist} - {pool_map.title}{version}"


def _pool_ref(pool_map: PoolMap) -> MapRef:
    return MapRef(pool_map.slot, _label(pool_map))


def _ref(event: MatchEvent, by_id: dict[int, PoolMap]) -> MapRef:
    pool_map = by_id.get(event.pool_map_id) if event.pool_map_id is not None else None
    if pool_map is not None:
        return _pool_ref(pool_map)
    return MapRef(UNKNOWN_MAP, f"Map {event.beatmap_id}" if event.beatmap_id else "Unknown map")


def _played(event: MatchEvent, by_id: dict[int, PoolMap], left_side: int, team_of) -> PlayedMap:
    scores = (event.team1_score, event.team2_score)
    left_score, right_score = scores if left_side == 1 else scores[::-1]
    return PlayedMap(_ref(event, by_id), team_of(event.winner_side), left_score, right_score)


def _current(beatmap_id: int | None, pool: list[PoolMap]) -> MapRef | None:
    if beatmap_id is None:
        return None
    pool_map = next((m for m in pool if m.beatmap_id == beatmap_id), None)
    return _pool_ref(pool_map) if pool_map else MapRef(UNKNOWN_MAP, f"Map {beatmap_id}")


def _why_none(
    view: matchup.MatchupView, fmt: SeriesFormat | None, current: MapRef | None
) -> str | None:
    if current is not None:
        return NOTICE_PLAYING
    if view.notice:
        return view.notice
    if view.format_notice:
        return view.format_notice
    return None if fmt else matchup.NO_FORMAT


def _next_picker(events: list[MatchEvent]) -> int | None:
    """The side that picks next, taking the picks to alternate from the last one with a known
    side; None while no pick has a side."""
    picks = [e for e in events if e.kind == "pick"]
    for index in range(len(picks) - 1, -1, -1):
        side = picks[index].team_side
        if side is not None:
            return side if (len(picks) - index) % 2 == 0 else 3 - side
    return None


def _suggest(
    conn: sqlite3.Connection,
    tournament: Tournament,
    match: Match,
    view: matchup.MatchupView,
    fmt: SeriesFormat | None,
    events: list[MatchEvent],
    score: tuple[int, int, int],
    taken: set[int | None],
) -> tuple[NextPick | None, float | None]:
    """The next pick the simulation makes from the score so far, and the series chance."""
    left_side, left_wins, right_wins = score
    if view.prediction is None or view.left is None or view.right is None or fmt is None:
        return None, None
    maps = [
        SeriesMap(c.pool_map.id, c.pool_map.bracket, c.prob, c.pool_map.is_tiebreaker)
        for c in view.prediction.comparisons
        if c.pool_map.id not in taken
    ]
    picker = _next_picker(events)
    picker_known = picker is not None
    picker_sim = 1 if picker is None or picker == left_side else 2
    habits = (
        history.habits(conn, tournament.id, view.left.id, match.id, match.scheduled_at),
        history.habits(conn, tournament.id, view.right.id, match.id, match.scheduled_at),
    )
    result = prediction.simulate_series(
        maps, fmt.best_of, 0, 0, picker_sim, habits, played=(left_wins, right_wins)
    )
    by_id = {c.pool_map.id: c for c in view.prediction.comparisons}
    needed = fmt.best_of // 2 + 1
    tiebreaker = next((c for c in by_id.values() if c.pool_map.is_tiebreaker), None)
    if left_wins == right_wins == needed - 1 and tiebreaker and tiebreaker.pool_map.id not in taken:
        return NextPick(None, _pool_ref(tiebreaker.pool_map), None, True, True), result.win_prob
    picks = result.orders[0].picks
    if not picks:
        return None, result.win_prob
    side, map_id = picks[0]
    chosen = by_id[map_id]
    chance = None if chosen.prob is None else (chosen.prob if side == 1 else 1 - chosen.prob)
    name = view.left.name if side == 1 else view.right.name
    return NextPick(name, _pool_ref(chosen.pool_map), chance, False, picker_known), result.win_prob
