"""Roster page: view for every member, edits for captains and admins."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clients.osu import OsuClient, OsuError, OsuNotFound
from app.clock import Clock
from app.domain.models import Role
from app.domain.parsing import parse_uint
from app.repos import members as members_repo
from app.services.access import (
    ROLE_LABELS,
    effective_role,
    is_temp_captain_active,
    may_change_captain_role,
)
from app.services.members import (
    Actor,
    MemberError,
    add_guest,
    form_time,
    remove_member,
    update_member,
)
from app.web.deps import (
    TournamentAccess,
    captain_access,
    get_clock,
    get_db,
    get_osu,
    member_access,
    require_writable,
)
from app.web.error_pages import error_page
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}/members")

OSU_LOOKUP_FAILED = "osu! did not answer, so the person could not be looked up. Try again later."
UNKNOWN_OSU_USER = "No osu! user has this id."


def _actor(access: TournamentAccess) -> Actor:
    return Actor(
        access.user.osu_id,
        access.is_admin,
        may_change_captain_role(access.is_admin, access.member),
    )


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    clock: Clock,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    now = clock.now()
    can_set_captain = may_change_captain_role(access.is_admin, access.member)
    rows = []
    for member in members_repo.list_for_tournament(db, access.tournament.id):
        if member.role == "player" and member.temp_captain_until:
            temp_state = "active" if is_temp_captain_active(member, now) else "expired"
        else:
            temp_state = None
        rows.append(
            {
                "member": member,
                "effective_captain": effective_role(member, now) >= Role.CAPTAIN,
                "temp_state": temp_state,
                "temp_value": form_time(member.temp_captain_until),
                "is_self": member.osu_id == access.user.osu_id,
                # Taking the captain role from someone, or removing them, is a captain's call.
                "captain_locked": member.role == "captain" and not can_set_captain,
            }
        )
    context = {
        "access": access,
        "tournament": access.tournament,
        "nav_current": "members",
        "rows": rows,
        "can_edit": access.role >= Role.CAPTAIN,
        "archived": access.tournament.status == "archived",
        "role_options": list(ROLE_LABELS.items()),
        "role_options_without_captain": [
            option for option in ROLE_LABELS.items() if option[0] != "captain"
        ],
        "can_set_captain": can_set_captain,
        "role_labels": ROLE_LABELS,
        **extra,
    }
    return render(request, "pages/tournament/members.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def members_page(
    request: Request,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    return _page(request, access, db, clock)


@router.post("")
async def add_guest_route(
    request: Request,
    osu_id: str = Form(""),
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    osu: OsuClient = Depends(get_osu),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Add a guest by osu! id. The id is checked against osu! so the roster shows a real name."""
    entered = osu_id.strip()
    error: str | None = None
    status = 422
    profile = None
    osu_user_id = parse_uint(entered)
    if osu_user_id is None or not 0 < osu_user_id < 2**31:
        error = "Enter the osu! user id as a number, for example 2."
    else:
        try:
            profile = await osu.get_user(osu_user_id)
        except OsuNotFound:
            error = UNKNOWN_OSU_USER
        except OsuError:
            error, status = OSU_LOOKUP_FAILED, 502
    if profile is not None:
        try:
            add_guest(db, access.tournament.id, _actor(access), profile, clock.now())
        except MemberError as refused:
            error, status = refused.message, refused.status
        else:
            flash(request, "success", f"{profile.username} was added as a guest.")
            return RedirectResponse(f"/t/{access.tournament.id}/members", status_code=303)
    return _page(
        request,
        access,
        db,
        clock,
        status_code=status,
        add_error=error,
        add_entered=entered,
    )


@router.post("/{osu_id}")
async def update_member_route(
    request: Request,
    osu_id: int,
    role: str = Form(""),
    is_substitute: str = Form(""),
    temp_captain_until: str = Form(""),
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    try:
        updated = update_member(
            db,
            access.tournament.id,
            _actor(access),
            osu_id,
            role,
            bool(is_substitute),
            temp_captain_until,
        )
    except MemberError as refused:
        return _refused(request, access, db, clock, refused)
    flash(request, "success", f"Saved changes for {updated.username or updated.osu_id}.")
    return RedirectResponse(f"/t/{access.tournament.id}/members", status_code=303)


@router.post("/{osu_id}/remove")
async def remove_member_route(
    request: Request,
    osu_id: int,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    try:
        removed = remove_member(db, access.tournament.id, _actor(access), osu_id)
    except MemberError as refused:
        return _refused(request, access, db, clock, refused)
    flash(request, "success", f"{removed.username or removed.osu_id} was removed.")
    return RedirectResponse(f"/t/{access.tournament.id}/members", status_code=303)


def _refused(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    clock: Clock,
    refused: MemberError,
) -> Response:
    if refused.status in (403, 404):
        return error_page(request, refused.status, refused.message)
    return _page(request, access, db, clock, status_code=refused.status, row_error=refused.message)
