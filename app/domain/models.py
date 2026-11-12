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
    id: int
    name: str
    acronym: str | None
    status: str


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
