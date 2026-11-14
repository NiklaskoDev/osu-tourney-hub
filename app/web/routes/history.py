"""Match history: what was played against what was predicted, corrections by a captain, and the
habits of an opponent."""

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clients.osu import OsuClient, OsuError, OsuNotFound
from app.domain.models import Match
from app.repos import teams as teams_repo
from app.services import history
from app.services.history import HistoryError
from app.web.deps import (
    TournamentAccess,
    captain_access,
    get_db,
    get_osu,
    member_access,
    require_writable,
)
from app.web.errors import UserFacingError
from app.web.routes.matches import find_match
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}")

NO_LOBBY = "This match has no multiplayer link, so there is no lobby to read."
LOBBY_MISSING = "osu! has no lobby with this id, or it is private. Nothing was changed."
NOT_YOUR_MATCH = "Events can only be corrected for a match our team plays in."
OSU_DOWN = "osu! did not respond. Nothing was changed; try again in a few minutes."


def _correctable_match(db: sqlite3.Connection, access: TournamentAccess, mid: str) -> Match:
    """The match, when this person may change its history: an admin any match, a captain only
    one our team plays in (as for the plan)."""
    match = find_match(db, access, mid)
    if not may_correct(access, match):
        raise UserFacingError(409, NOT_YOUR_MATCH)
    return match


def may_correct(access: TournamentAccess, match: Match) -> bool:
    return access.is_admin or match.involves(access.tournament.our_team_id)


def _review_page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    match: Match,
    status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    tournament = access.tournament
    view = history.build_review(db, tournament, match)
    archived = tournament.status == "archived"
    sides = [("", "Unknown")]
    sides += [(str(side), team.name) for side, team in ((1, view.team1), (2, view.team2)) if team]
    context = {
        "access": access,
        "tournament": tournament,
        "nav_current": "schedule",
        "archived": archived,
        "view": view,
        "can_edit": access.can_manage and not archived and may_correct(access, match),
        "side_options": sides,
        "winner_options": [("", "No winner")] + sides[1:],
        "map_options": [("", "No map")]
        + [(str(m.id), f"{m.slot} {m.title or 'Map not announced'}") for m in view.pool],
        "kind_options": [("", "Add an event...")]
        + [(kind, label) for kind, label in history.KIND_LABELS.items()],
        "side_field": history.side_field,
        "map_field": history.map_field,
        "winner_field": history.winner_field,
        "reset_field": history.reset_field,
        "form_values": {},
        **extra,
    }
    return render(request, "pages/tournament/review.html", context, status_code=status_code)


@router.get("/matches/{mid}/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _review_page(request, access, db, find_match(db, access, mid))


@router.post("/matches/{mid}/review/refresh")
async def refresh_lobby(
    request: Request,
    mid: str,
    return_to: str = Form(""),
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    osu: OsuClient = Depends(get_osu),
) -> Response:
    """Read the match's lobby again and rebuild the picks from it. `return_to=stats` goes back to
    the stats page, anything else to the review page."""
    match = _correctable_match(db, access, mid)
    if match.mp_match_id is None:
        raise UserFacingError(409, NO_LOBBY)
    try:
        count = await history.refresh_from_lobby(db, osu, match, request.app.state.clock.now())
    except OsuNotFound:
        flash(request, "error", LOBBY_MISSING)
    except OsuError:
        flash(request, "error", OSU_DOWN)
    else:
        flash(request, "success", f"Lobby read: {count} played maps found.")
    page = "stats" if return_to == "stats" else "review"
    return RedirectResponse(f"/t/{access.tournament.id}/matches/{match.id}/{page}", 303)


@router.post("/matches/{mid}/events")
async def correct_events(
    request: Request,
    mid: str,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    match = _correctable_match(db, access, mid)
    fields = await request.form()
    values = {key: value for key, value in fields.items() if isinstance(value, str)}
    try:
        changed = history.save_corrections(db, match, values)
    except HistoryError as refused:
        return _review_page(
            request,
            access,
            db,
            match,
            422,
            form_values=values,
            form_error=refused.message,
            form_error_field=refused.field,
        )
    flash(request, "success", "Events saved." if changed else "Nothing was changed.")
    return RedirectResponse(f"/t/{access.tournament.id}/matches/{match.id}/review", 303)


@router.get("/opponents/{team_id}", response_class=HTMLResponse)
async def opponent_page(
    request: Request,
    team_id: int,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    team = teams_repo.get(db, team_id)
    if team is None or team.tournament_id != access.tournament.id:
        raise UserFacingError(404, "This team is not in this tournament.")
    context = {
        "access": access,
        "tournament": access.tournament,
        "nav_current": "schedule",
        "view": history.build_opponent(db, access.tournament, team),
    }
    return render(request, "pages/tournament/opponent.html", context)
