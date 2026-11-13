"""Dataclasses shared by the layers. No I/O here."""

from dataclasses import dataclass
from enum import IntEnum


@dataclass(frozen=True)
class OsuProfile:
    """The public osu! profile fields the app keeps."""

    osu_id: int
    username: str
    avatar_url: str | None
    country: str | None


@dataclass(frozen=True)
class User:
    osu_id: int
    username: str
    avatar_url: str | None
    country: str | None
    timezone: str | None
    session_version: int
    created_at: str
    last_login_at: str | None


class Role(IntEnum):
    """What someone may do in a tournament. Higher includes everything below it."""

    GUEST = 0
    PLAYER = 1
    CAPTAIN = 2
    ADMIN = 3


MEMBER_ROLES = ("captain", "co_captain", "player", "guest")
TOURNAMENT_STATUSES = ("active", "archived", "hidden")


@dataclass(frozen=True)
class Tournament:
    """A tournament row. Pages that only list tournaments leave the later fields at defaults."""

    id: int
    name: str
    acronym: str | None
    status: str
    gamemode: int = 0
    is_solo: bool = False
    team_size_min: int | None = None
    team_size_max: int | None = None
    freemod_rule: str = "none"
    scoring: str = "v2"
    owner_osu_id: int | None = None
    source: str | None = None
    wybin_id: int | None = None
    slug: str | None = None
    final_placing: str | None = None
    our_team_id: int | None = None
    last_synced_at: str | None = None


FREEMOD_RULES = ("none", "each_player_mod", "two_distinct_mods")
SCORING_MODES = ("v2", "v1", "lazer")


@dataclass(frozen=True)
class Stage:
    id: int
    tournament_id: int
    wybin_stage_id: int | None
    name: str
    sort_order: int
    start_at: str | None
    end_at: str | None
    best_of: int | None
    bans: int | None
    protects: int | None
    is_qualifier: bool
    lineup_size: int | None
    pool_released: bool

    @property
    def format_text(self) -> str:
        """ "Best of 9, 1 ban, 2 protects" from whatever the stage defines."""
        parts = []
        if self.best_of:
            parts.append(f"Best of {self.best_of}")
        for count, noun in ((self.bans, "ban"), (self.protects, "protect")):
            if count:
                parts.append(f"{count} {noun}{'s' if count != 1 else ''}")
        return ", ".join(parts)


@dataclass(frozen=True)
class PoolMap:
    id: int
    stage_id: int
    slot: str
    bracket: str
    required_mods: str
    is_freemod: bool
    is_tiebreaker: bool
    sort_order: int
    beatmap_id: int | None
    beatmapset_id: int | None
    artist: str | None
    title: str | None
    version: str | None
    mapper: str | None
    star_rating: float | None
    bpm: float | None
    length_s: int | None
    cs: float | None
    ar: float | None
    od: float | None
    hp: float | None


@dataclass(frozen=True)
class BeatmapMeta:
    """Public metadata of one beatmap, as osu! reports it."""

    beatmap_id: int
    beatmapset_id: int | None
    artist: str | None
    title: str | None
    version: str | None
    mapper: str | None
    star_rating: float | None
    bpm: float | None
    length_s: int | None
    cs: float | None
    ar: float | None
    od: float | None
    hp: float | None


@dataclass(frozen=True)
class Team:
    id: int
    tournament_id: int
    wybin_team_id: int | None
    name: str
    is_ours: bool
    seed: int | None


@dataclass(frozen=True)
class TeamPlayer:
    team_id: int
    osu_id: int
    username: str
    timezone_iana: str | None
    timezone_offset_min: int | None
    is_captain: bool


@dataclass(frozen=True)
class Member:
    """A roster entry. `username` comes from the users table when that person has been seen."""

    tournament_id: int
    osu_id: int
    username: str | None
    avatar_url: str | None
    role: str
    is_substitute: bool
    temp_captain_until: str | None
    source: str | None


@dataclass(frozen=True)
class Score:
    """One score of one player on one beatmap. `mods` is canonical; `accuracy` is 0 to 100."""

    id: int
    beatmap_id: int
    osu_id: int
    gamemode: int
    mods: str
    score: int
    scoring: str
    accuracy: float
    max_combo: int | None
    misses: int | None
    passed: bool
    achieved_at: str | None
    source: str
    source_ref: str
    source_url: str | None
    entered_by: int | None
    tournament_id: int | None
    created_at: str

    @property
    def played_at(self) -> str:
        """When it was played; a hand-entered score without a date counts from when it was added."""
        return self.achieved_at or self.created_at


@dataclass(frozen=True)
class ApiScore:
    """A passed score as the osu! API reports it, already in the app's units: `mods` canonical,
    `accuracy` 0 to 100, `total_score` the lazer standardised score."""

    score_id: int
    beatmap_id: int
    osu_id: int
    mods: str
    total_score: int
    accuracy: float
    max_combo: int | None
    misses: int | None
    achieved_at: str | None


@dataclass(frozen=True)
class Note:
    id: int
    tournament_id: int
    pool_map_id: int
    author_osu_id: int
    visibility: str
    body: str
    updated_at: str


@dataclass(frozen=True)
class Lineup:
    """The saved lineup of a stage (`match_id` stays empty until matches exist)."""

    id: int
    tournament_id: int
    stage_id: int
    match_id: int | None
    status: str
    updated_by: int | None
    updated_at: str


@dataclass(frozen=True)
class LineupEntry:
    pool_map_id: int
    osu_id: int
    mods: str
