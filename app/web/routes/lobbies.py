"""Lobbies where pool maps were played: found by the scanner, linked on wybin or pasted here."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clients.osu import OsuClient, OsuError, OsuNotFound
from app.clock import Clock
from app.domain.models import Role
from app.domain.parsing import parse_uint
from app.repos import lobbies as lobbies_repo
from app.repos import stages as stages_repo
from app.services import lobby_list, pool_sheet
from app.services.links import match_id
from app.services.lobbies import import_lobby
from app.web.deps import (
    TournamentAccess,
    get_clock,
    get_db,
    get_osu,
    member_access,
    player_access,
    require_writable,
)
from app.web.errors import UserFacingError
from app.web.security import SlidingWindowLimiter
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/lobbies")

NOT_A_LINK = "Paste an osu! multiplayer link, such as https://osu.ppy.sh/community/matches/123456."
NOT_FOUND = "osu! has no lobby with this id, or it is private."
OSU_DOWN = "osu! did not respond. Nothing was saved; try again in a few minutes."


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    stage_ref: str | None = "",
    player_ref: str = "",
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tournament = access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    stage_id = next((s.id for s in stages if str(s.id) == stage_ref), None)
    if stage_ref is None:
        # No `stage` in the query at all: an active tournament opens on the current round. An empty
        # `stage=` is the user's choice of "All stages".
        now = pool_sheet.stage_clock_now(tournament, request.app.state.clock)
        current = pool_sheet.current_stage(stages, now) if now is not None else None
        stage_id = current.id if current else None
    player_id = parse_uint(player_ref)
    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "lobbies",
        "archived": tournament.status == "archived",
        "can_add": access.role >= Role.PLAYER,
        "stage_options": [("", "All stages"), *((str(s.id), s.name) for s in stages)],
        "player_options": [("", "Everyone"), *lobby_list.player_options(db, tournament)],
        "stage_value": "" if stage_id is None else str(stage_id),
        "player_value": "" if player_id is None else str(player_id),
        "filtered": stage_id is not None or player_id is not None,
        "rows": lobby_list.list_lobbies(db, tournament, stage_id, player_id),
        "limit": lobby_list.LOBBY_LIMIT,
        **extra,
    }
    return render(request, "pages/tournament/lobbies.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def lobbies_page(
    request: Request,
    stage: str | None = None,
    player: str = "",
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, access, db, stage, player)


@router.post("")
async def add_lobby(
    request: Request,
    link: str = Form(""),
    access: TournamentAccess = Depends(player_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    osu: OsuClient = Depends(get_osu),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Read a lobby a member pasted and store it for this tournament, hits or not."""
    entered = link.strip()
    mp_id = match_id(entered)
    if mp_id is None:
        return _refused(request, access, db, 422, NOT_A_LINK, entered)
    existing = lobbies_repo.get(db, mp_id)
    if existing is not None and existing.is_complete:
        flash(request, "info", f"Lobby {mp_id} is already saved.")
        return _back(access)
    _limit(request, str(access.user.osu_id))
    try:
        result = await import_lobby(db, osu, mp_id, access.tournament.id, "manual", clock.now())
    except OsuNotFound:
        return _refused(request, access, db, 422, NOT_FOUND, entered)
    except OsuError:
        return _refused(request, access, db, 502, OSU_DOWN, entered)
    found = "no pool map" if result.hit_games == 0 else f"{result.hit_games} games on pool maps"
    flash(request, "success", f"Lobby {mp_id} saved: {found} found.")
    return _back(access)


def _refused(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    status: int,
    message: str,
    entered: str,
) -> HTMLResponse:
    return _page(request, access, db, status_code=status, link_error=message, link_entered=entered)


def _back(access: TournamentAccess) -> RedirectResponse:
    return RedirectResponse(f"/t/{access.tournament.id}/lobbies", status_code=303)


def _limit(request: Request, key: str) -> None:
    limiter: SlidingWindowLimiter = request.app.state.lobby_link_limiter
    if not limiter.allow(key):
        wait = limiter.retry_after_seconds(key)
        minutes = max(1, round(wait / 60))
        raise UserFacingError(
            429,
            f"You added many lobbies lately. Try again in about {minutes} minutes.",
            headers={"Retry-After": str(wait)},
        )
