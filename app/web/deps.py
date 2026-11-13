"""Request dependencies: database, current user, and the access checks every route goes through.

Order of checks for a tournament route: logged in (else redirect to login) -> member or admin
(else 404, so a tournament's existence is not revealed) -> role (else 403) -> not archived for
writes (else 409). Everything is denied unless a rule allows it.

The SQLite calls are short and run on the event loop thread, so never `await` while a transaction
is open.
"""

import sqlite3
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, Request

from app.clients.osu import OsuClient
from app.clients.wybin import WybinClient
from app.clock import Clock
from app.config import Settings
from app.db.connection import open_db
from app.domain.models import Member, Role, Tournament, User
from app.repos import members as members_repo
from app.repos import tournaments as tournaments_repo
from app.repos import users as users_repo
from app.services.access import role_for
from app.web.errors import UserFacingError
from app.web.session import OSU_ID_KEY, VERSION_KEY

ARCHIVED_MESSAGE = "This tournament is archived. An admin can unarchive it."


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_osu(request: Request) -> OsuClient:
    return request.app.state.osu


def get_wybin(request: Request) -> WybinClient:
    return request.app.state.wybin


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


async def get_db(request: Request) -> AsyncIterator[sqlite3.Connection]:
    conn = open_db(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def load_session_user(request: Request, conn: sqlite3.Connection) -> User | None:
    """The user the session cookie names, or None. A stale cookie (unknown user, or a
    `session_version` that was bumped by "log out everywhere" or deletion) is dropped."""
    osu_id = request.session.get(OSU_ID_KEY)
    version = request.session.get(VERSION_KEY)
    if not isinstance(osu_id, int) or not isinstance(version, int):
        return None
    user = users_repo.get(conn, osu_id)
    if user is None or user.session_version != version:
        request.session.clear()
        return None
    return user


async def current_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> User | None:
    user = load_session_user(request, db)
    request.state.user = user
    return user


async def require_login(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise UserFacingError(401, "Log in with osu! to continue.")
    return user


def is_admin(user: User, settings: Settings) -> bool:
    """Admins are the osu! ids in `ADMIN_OSU_IDS`, judged on every request."""
    return user.osu_id in settings.admin_osu_ids


async def require_admin(
    user: User = Depends(require_login), settings: Settings = Depends(get_settings)
) -> User:
    if not is_admin(user, settings):
        raise UserFacingError(403, "Only administrators can open this page.")
    return user


@dataclass(frozen=True)
class TournamentAccess:
    user: User
    tournament: Tournament
    member: Member | None
    role: Role

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def can_manage(self) -> bool:
        """Captains and above may change the tournament's settings."""
        return self.role >= Role.CAPTAIN


async def tournament_access(
    tid: int,
    user: User = Depends(require_login),
    db: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    clock: Clock = Depends(get_clock),
) -> TournamentAccess:
    """Resolve the viewer's access to `/t/{tid}/...`. Unknown, hidden and foreign tournaments are
    all the same 404."""
    tournament = tournaments_repo.get(db, tid)
    admin = is_admin(user, settings)
    if tournament is None or (tournament.status == "hidden" and not admin):
        raise UserFacingError(404, "This page does not exist, or you do not have access to it.")
    member = members_repo.get(db, tid, user.osu_id)
    role = role_for(admin, member, clock.now())
    if role is None:
        raise UserFacingError(404, "This page does not exist, or you do not have access to it.")
    return TournamentAccess(user, tournament, member, role)


def require_member(
    min_role: Role = Role.GUEST,
) -> Callable[..., Coroutine[Any, Any, TournamentAccess]]:
    """Dependency factory: the viewer must belong to the tournament with at least `min_role`."""

    async def dependency(access: TournamentAccess = Depends(tournament_access)) -> TournamentAccess:
        if access.role < min_role:
            raise UserFacingError(403, "You do not have permission to do this.")
        return access

    return dependency


# One shared dependency per level, so FastAPI evaluates each at most once per request.
member_access = require_member(Role.GUEST)
player_access = require_member(Role.PLAYER)
captain_access = require_member(Role.CAPTAIN)


async def require_writable(
    access: TournamentAccess = Depends(tournament_access),
) -> TournamentAccess:
    """Write routes list this after `require_member`, so the role is checked first."""
    if access.tournament.status == "archived":
        raise UserFacingError(409, ARCHIVED_MESSAGE)
    return access
