"""What the pool and lineup pages show for one stage: members, scores, the best per player and
map, averages, and the suggested lineup. Loads from the repos and uses the pure rules in
`sheet` and `lineup`."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.domain.models import Member, Note, PoolMap, Score, Stage, Tournament
from app.domain.parsing import parse_uint
from app.repos import members as members_repo
from app.repos import notes as notes_repo
from app.repos import scores as scores_repo
from app.repos import stages as stages_repo
from app.repos import tournaments as tournaments_repo
from app.services import lineup, sheet
from app.services.tournament_settings import SCORING_LABELS, effective_lineup_size

SOURCE_LABELS = {
    "manual": "Added by hand",
    "api": "osu! API",
    "mp": "Multiplayer lobby",
    "import": "Imported",
}
HAND_ENTERED = ("manual", "import")
OSU_LINK_PREFIX = "https://osu.ppy.sh/"
FROM_ANOTHER_TOURNAMENT = "from another tournament"
OTHER_SCORING = "Other scoring type"


@dataclass(frozen=True)
class StageData:
    """Everything needed to judge one stage: its maps, the roster that plays, and their scores."""

    stage: Stage
    pool: list[PoolMap]
    players: list[Member]  # everyone but guests
    scores: list[Score]
    best: dict[sheet.BestKey, sheet.BestScore]


@dataclass(frozen=True)
class Viewer:
    osu_id: int
    can_manage: bool
    tournament_names: dict[int, str]  # tournaments the viewer may see, for "from <name>" hints


@dataclass(frozen=True)
class Cell:
    score: Score | None
    old: bool


@dataclass(frozen=True)
class MapRow:
    pool_map: PoolMap
    cells: list[Cell]
    team_score: float | None
    team_accuracy: float | None
    team_count: int


@dataclass(frozen=True)
class AverageRow:
    label: str
    cells: list[sheet.Average]
    team: sheet.Average


@dataclass(frozen=True)
class ScoreLine:
    score: Score
    player: str
    status: str  # "Best", "Counts", or why it does not count
    old: bool
    source: str
    link: str | None
    hint: str | None
    can_delete: bool


@dataclass(frozen=True)
class NoteLine:
    note: Note
    author: str
    mine: bool


@dataclass(frozen=True)
class MapDetail:
    pool_map: PoolMap
    scores: list[ScoreLine]
    notes: list[NoteLine]


@dataclass(frozen=True)
class PoolView:
    columns: list[Member]
    rows: list[MapRow]
    averages: list[AverageRow]
    details: list[MapDetail]


def visible_tournament_names(
    conn: sqlite3.Connection, osu_id: int, is_admin: bool
) -> dict[int, str]:
    """Names of the tournaments this person may see: all of them for an admin, else their own
    non-hidden ones."""
    entries = (
        tournaments_repo.list_all(conn, osu_id)
        if is_admin
        else tournaments_repo.list_for_member(conn, osu_id)
    )
    return {entry.tournament.id: entry.tournament.name for entry in entries}


def pick_stage(stages: list[Stage], raw: str | None) -> Stage | None:
    """The stage named by `?stage=` if it is one of these; else the first stage with a published
    pool, else the first stage."""
    wanted = parse_uint(raw or "")
    for stage in stages:
        if stage.id == wanted:
            return stage
    return next((stage for stage in stages if stage.pool_released), stages[0] if stages else None)


def load_stage(conn: sqlite3.Connection, tournament: Tournament, stage: Stage) -> StageData:
    pool = stages_repo.list_maps(conn, stage.id)
    players = [
        member
        for member in members_repo.list_for_tournament(conn, tournament.id)
        if member.role != "guest"
    ]
    beatmaps = {pool_map.beatmap_id for pool_map in pool if pool_map.beatmap_id is not None}
    scores = scores_repo.list_for_beatmaps(
        conn, beatmaps, [player.osu_id for player in players], tournament.gamemode
    )
    best = sheet.best_scores(pool, scores, tournament.scoring)
    return StageData(stage, pool, players, scores, best)


def suggest_for_stage(
    data: StageData, tournament: Tournament, include_substitutes: bool
) -> dict[int, lineup.MapSuggestion]:
    players = lineup.eligible_players(data.players, include_substitutes)
    size = effective_lineup_size(data.stage, tournament)
    return lineup.suggest(data.pool, data.best, players, size, tournament.freemod_rule)


def build_pool_view(
    conn: sqlite3.Connection,
    tournament: Tournament,
    data: StageData,
    viewer: Viewer,
    now: datetime,
) -> PoolView:
    ids = [player.osu_id for player in data.players]
    names = {player.osu_id: _name(player) for player in data.players}
    notes = notes_repo.list_visible(
        conn, tournament.id, [pool_map.id for pool_map in data.pool], viewer.osu_id
    )
    details = [
        MapDetail(
            pool_map,
            _score_lines(pool_map, data, tournament, viewer, names, now),
            _note_lines([n for n in notes if n.pool_map_id == pool_map.id], names, viewer),
        )
        for pool_map in data.pool
    ]
    rows = [_map_row(pool_map, data, ids, now) for pool_map in data.pool]
    return PoolView(data.players, rows, _average_rows(data, ids), details)


def _name(member: Member) -> str:
    return member.username or f"osu! user {member.osu_id}"


def _note_lines(notes: list[Note], names: dict[int, str], viewer: Viewer) -> list[NoteLine]:
    return [
        NoteLine(
            note,
            names.get(note.author_osu_id, "Former member"),
            note.author_osu_id == viewer.osu_id,
        )
        for note in notes
    ]


def _map_row(pool_map: PoolMap, data: StageData, ids: list[int], now: datetime) -> MapRow:
    cells = []
    for osu_id in ids:
        found = data.best.get((osu_id, pool_map.id))
        cells.append(
            Cell(found.overall, sheet.is_old(found.overall, now)) if found else Cell(None, False)
        )
    team_score, team_accuracy, count = sheet.map_average(pool_map, data.best, ids)
    return MapRow(pool_map, cells, team_score, team_accuracy, count)


def _average_rows(data: StageData, ids: list[int]) -> list[AverageRow]:
    brackets = list(dict.fromkeys(pool_map.bracket for pool_map in data.pool))
    groups = [("All maps", data.pool)]
    if len(brackets) > 1:  # one bracket would only repeat the overall row
        groups += [(b, [m for m in data.pool if m.bracket == b]) for b in brackets]
    return [
        AverageRow(
            label,
            [sheet.personal_average(maps, data.best, osu_id) for osu_id in ids],
            sheet.team_average(maps, data.best, ids),
        )
        for label, maps in groups
    ]


def _score_lines(
    pool_map: PoolMap,
    data: StageData,
    tournament: Tournament,
    viewer: Viewer,
    names: dict[int, str],
    now: datetime,
) -> list[ScoreLine]:
    on_map = [s for s in data.scores if s.beatmap_id == pool_map.beatmap_id]
    on_map.sort(key=sheet.rank_key, reverse=True)
    on_map.sort(key=lambda s: (names.get(s.osu_id, ""), s.osu_id))  # stable: best first per player
    return [
        ScoreLine(
            score=score,
            player=names.get(score.osu_id, f"osu! user {score.osu_id}"),
            status=_status(pool_map, score, data, tournament),
            old=sheet.is_old(score, now),
            source=SOURCE_LABELS.get(score.source, score.source),
            link=_safe_link(score.source_url),
            hint=_origin_hint(score, tournament, viewer),
            can_delete=_can_delete(score, tournament, viewer),
        )
        for score in on_map
    ]


def _status(pool_map: PoolMap, score: Score, data: StageData, tournament: Tournament) -> str:
    reason = sheet.uncounted_reason(pool_map, score, tournament.scoring)
    if reason == OTHER_SCORING:
        used = SCORING_LABELS.get(score.scoring, "Unknown scoring")
        return f"Not counted: {used} score"
    if reason:
        return reason
    found = data.best.get((score.osu_id, pool_map.id))
    return "Best" if found and found.overall.id == score.id else "Counts"


def _safe_link(url: str | None) -> str | None:
    return url if url and url.startswith(OSU_LINK_PREFIX) else None


def _origin_hint(score: Score, tournament: Tournament, viewer: Viewer) -> str | None:
    """A "from <tournament>" hint for a hand-entered score typed in another tournament. The name
    is only shown to people who can see that tournament themselves."""
    if score.source not in HAND_ENTERED or score.tournament_id == tournament.id:
        return None
    name = viewer.tournament_names.get(score.tournament_id) if score.tournament_id else None
    return f"from {name}" if name else FROM_ANOTHER_TOURNAMENT


def _can_delete(score: Score, tournament: Tournament, viewer: Viewer) -> bool:
    if score.source not in HAND_ENTERED:
        return False
    return score.osu_id == viewer.osu_id or (
        viewer.can_manage and score.tournament_id == tournament.id
    )
