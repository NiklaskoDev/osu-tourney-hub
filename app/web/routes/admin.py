"""Admin console: wybin account link, import, manual tournaments, archive, hide, sync, delete, AI
status."""

import sqlite3
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clients.ai import AiProvider
from app.clients.wybin import InvalidWybinLink, WybinClient
from app.clients.wybin_models import WybinError, WybinFormatError, WybinNotFound
from app.clock import Clock
from app.config import Settings
from app.domain.models import Tournament, User
from app.domain.modes import GAME_MODES, mode_name
from app.domain.parsing import parse_uint
from app.repos import jobs as jobs_repo
from app.repos import tournaments as tournaments_repo
from app.repos import wybin_links as links_repo
from app.services import ai_prediction
from app.services import archive as archive_service
from app.services.archive import ArchiveError
from app.services.tournament_setup import SetupError, TournamentForm, create_manual
from app.services.wybin_sync import (
    TournamentArchivedError,
    import_tournament,
    sync_tournament,
)
from app.web.deps import get_ai, get_clock, get_db, get_wybin, require_admin
from app.web.errors import UserFacingError
from app.web.session import flash, has_recent_login
from app.web.templating import render
from app.workers.jobs import CONTINUOUS_JOBS, JOB_LABELS, SKIMMER_JOBS, TRACK_SIGNUPS
from app.workers.scheduler import Scheduler

router = APIRouter(prefix="/admin")

WYBIN_DOWN = "wybin did not respond. Nothing was changed; try again later."
WYBIN_UNREADABLE = "wybin sent data this app cannot read. Nothing was changed."
MAX_WYBIN_USER_ID = 2**31
NOT_FROM_WYBIN_MESSAGE = "This tournament did not come from wybin."
POOLS_UNREADABLE_WARNING = (
    "Teams and stages were updated; the map pool could not be read. Try syncing again later."
)
MATCHES_UNREADABLE_WARNING = (
    "Teams, stages and the map pool were updated; the matches could not be read. "
    "Try syncing again later."
)
ARCHIVED_SYNC_MESSAGE = "This tournament is archived. Restore it before syncing."
STATUS_LABELS = {"active": "Active", "archived": "Archived", "hidden": "Hidden"}


def _page(
    request: Request,
    user: User,
    db: sqlite3.Connection,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    rows = [
        {
            "tournament": entry.tournament,
            "mode": mode_name(entry.tournament.gamemode),
            "status": STATUS_LABELS[entry.tournament.status],
            "from_wybin": entry.tournament.source == "wybin",
            "confirm_text": entry.tournament.acronym or entry.tournament.name,
        }
        for entry in tournaments_repo.list_all(db, user.osu_id)
    ]
    jobs = []
    scanner_on = request.app.state.settings.scanner_enabled
    for name, label in JOB_LABELS.items():
        if name in SKIMMER_JOBS and not scanner_on:
            continue
        state = jobs_repo.get(db, name)
        jobs.append(
            {
                "label": label,
                "last_run_at": state.last_run_at if state else None,
                "last_ok_at": state.last_ok_at if state else None,
                "error": state.last_error if state else None,
                "continuous": name in CONTINUOUS_JOBS,
            }
        )
    context = {
        "nav_current": "admin",
        "rows": rows,
        "jobs": jobs,
        "link": links_repo.get(db, user.osu_id),
        "mode_options": [(str(code), name) for code, name in GAME_MODES.items()],
        "ai": _ai_info(request.app.state.settings),
        "ai_share_usernames": ai_prediction.share_usernames(db),
        **extra,
    }
    return render(request, "pages/admin/index.html", context, status_code=status_code)


def _ai_info(settings: Settings) -> dict:
    """What the page may know of the AI setup. Never the whole settings: they hold the API key."""
    return {
        "enabled": settings.ai_enabled,
        "provider": settings.ai_provider,
        "model": settings.ai_model,
        "missing": settings.ai_missing,
    }


def _tournament(db: sqlite3.Connection, tid: int) -> Tournament:
    tournament = tournaments_repo.get(db, tid)
    if tournament is None:
        raise UserFacingError(404, "This tournament does not exist.")
    return tournament


def _wybin_failure(error: WybinError) -> tuple[str, int]:
    if isinstance(error, WybinFormatError):
        return WYBIN_UNREADABLE, 502
    return WYBIN_DOWN, 502


@router.get("", response_class=HTMLResponse)
async def admin_page(
    request: Request, user: User = Depends(require_admin), db: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    return _page(request, user, db)


@router.post("/wybin-link")
async def link_wybin_account(
    request: Request,
    background: BackgroundTasks,
    wybin_user_id: str = Form(""),
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    wybin: WybinClient = Depends(get_wybin),
) -> Response:
    """Remember the admin's wybin account, then check for sign-ups right away."""
    entered = wybin_user_id.strip()
    error: str | None = None
    status = 422
    wybin_id = parse_uint(entered)
    if wybin_id is None or not 0 < wybin_id < MAX_WYBIN_USER_ID:
        error = "Enter your wybin user id as a number."
    else:
        try:
            await wybin.get_user_detailed(wybin_id)
        except WybinNotFound:
            error = "wybin has no user with this id."
        except WybinError as failure:
            error, status = _wybin_failure(failure)
    if error is None:
        try:
            links_repo.upsert(db, user.osu_id, wybin_id)
        except sqlite3.IntegrityError:
            error, status = "Another admin already uses this wybin account.", 409
    if error is not None:
        return _page(request, user, db, status, link_error=error, link_entered=entered)
    scheduler: Scheduler = request.app.state.scheduler
    background.add_task(scheduler.run_once, scheduler.job(TRACK_SIGNUPS))
    flash(request, "success", "wybin account linked. Tournaments you signed up for are imported.")
    return RedirectResponse("/admin", status_code=303, background=background)


@router.post("/wybin-link/remove")
async def unlink_wybin_account(
    request: Request, user: User = Depends(require_admin), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    links_repo.remove(db, user.osu_id)
    flash(request, "success", "wybin account unlinked. Tournaments already imported stay.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/ai-settings")
async def save_ai_settings(
    request: Request,
    share_usernames: str = Form(""),
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    ai: AiProvider = Depends(get_ai),
) -> Response:
    """Whether usernames and team names may be sent to the AI provider (off by default). Like the
    rest of the AI feature it does not exist while the provider is off."""
    if not ai.enabled:
        raise UserFacingError(404, "AI predictions are off.")
    ai_prediction.set_share_usernames(db, bool(share_usernames))
    flash(request, "success", "AI setting saved.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/tournaments/import")
async def import_from_wybin(
    request: Request,
    link: str = Form(""),
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    wybin: WybinClient = Depends(get_wybin),
    clock: Clock = Depends(get_clock),
) -> Response:
    error: str | None = None
    status = 422
    try:
        result = await import_tournament(db, wybin, link, user.osu_id, clock)
    except InvalidWybinLink as invalid:
        error = str(invalid)
    except TournamentArchivedError:
        error, status = ARCHIVED_SYNC_MESSAGE, 409
    except WybinNotFound:
        error = "wybin has no tournament with this address."
    except WybinError as failure:
        error, status = _wybin_failure(failure)
    else:
        tournament = _tournament(db, result.tournament_id)
        if result.pools_unreadable:
            flash(request, "warning", POOLS_UNREADABLE_WARNING)
        elif result.matches_unreadable:
            flash(request, "warning", MATCHES_UNREADABLE_WARNING)
        else:
            verb = "imported" if result.created else "was already imported and is up to date"
            flash(request, "success", f"{tournament.name} {verb}.")
        return RedirectResponse(f"/t/{result.tournament_id}", status_code=303)
    return _page(request, user, db, status, import_error=error, import_entered=link.strip())


@router.post("/tournaments")
async def create_tournament(
    request: Request,
    name: str = Form(""),
    acronym: str = Form(""),
    gamemode: str = Form(""),
    is_solo: str = Form(""),
    team_size_min: str = Form(""),
    team_size_max: str = Form(""),
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    form = TournamentForm(name, acronym, gamemode, bool(is_solo), team_size_min, team_size_max)
    try:
        tournament_id = create_manual(db, user, form, clock.now())
    except SetupError as refused:
        values = {
            "name": name,
            "acronym": acronym,
            "gamemode": gamemode,
            "is_solo": bool(is_solo),
            "team_size_min": team_size_min,
            "team_size_max": team_size_max,
        }
        return _page(
            request,
            user,
            db,
            refused.status,
            create_error=refused.message,
            create_field=refused.field,
            create_values=values,
        )
    flash(request, "success", "Tournament created. Add its stages and map pool below.")
    return RedirectResponse(f"/admin/tournaments/{tournament_id}", status_code=303)


@router.post("/tournaments/{tid}/archive")
async def archive_tournament(
    request: Request,
    tid: int,
    final_placing: str = Form(""),
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    try:
        tournament = archive_service.archive(db, tid, final_placing, clock.now())
    except ArchiveError as refused:
        return _refused(request, user, db, refused)
    flash(request, "success", f"{tournament.name} is archived and now read-only.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/tournaments/{tid}/unarchive")
async def restore_tournament(
    request: Request,
    tid: int,
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        tournament = archive_service.restore(db, tid)
    except ArchiveError as refused:
        return _refused(request, user, db, refused)
    flash(request, "success", f"{tournament.name} is active again.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/tournaments/{tid}/hide")
async def hide_tournament(
    request: Request,
    tid: int,
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    try:
        tournament = archive_service.hide(db, tid)
    except ArchiveError as refused:
        return _refused(request, user, db, refused)
    flash(request, "success", f"{tournament.name} is hidden from everyone but administrators.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/tournaments/{tid}/sync")
async def sync_now(
    request: Request,
    tid: int,
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    wybin: WybinClient = Depends(get_wybin),
    clock: Clock = Depends(get_clock),
) -> Response:
    tournament = _tournament(db, tid)
    if tournament.source != "wybin":
        return _page(
            request, user, db, 409, action_error="This tournament did not come from wybin."
        )
    if tournament.status == "archived":
        return _page(request, user, db, 409, action_error=ARCHIVED_SYNC_MESSAGE)
    try:
        result = await sync_tournament(db, wybin, tid, clock)
    except WybinError as failure:
        message, status = _wybin_failure(failure)
        return _page(request, user, db, status, action_error=message)
    if result.pools_unreadable:
        flash(request, "warning", POOLS_UNREADABLE_WARNING)
    elif result.matches_unreadable:
        flash(request, "warning", MATCHES_UNREADABLE_WARNING)
    else:
        flash(request, "success", f"{_tournament(db, tid).name} is up to date with wybin.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/tournaments/{tid}/delete")
async def delete_tournament(
    request: Request,
    tid: int,
    confirm: str = Form(""),
    user: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Deleting everything of a tournament needs a login from the last five minutes and the
    acronym (or the name, when there is no acronym) typed as confirmation."""
    tournament = _tournament(db, tid)
    if not has_recent_login(request, clock.now()):
        return RedirectResponse("/auth/login?next=/admin", status_code=303)
    expected = tournament.acronym or tournament.name
    if confirm.strip() != expected:
        return _page(
            request,
            user,
            db,
            422,
            action_error=f"Nothing was deleted: type {expected} exactly to confirm.",
        )
    archive_service.delete_tournament(db, tid)
    flash(request, "success", f"{tournament.name} and all its data were deleted.")
    return RedirectResponse("/admin", status_code=303)


def _refused(
    request: Request, user: User, db: sqlite3.Connection, refused: ArchiveError
) -> Response:
    if refused.status == 404:
        raise UserFacingError(404, refused.message)
    return _page(request, user, db, refused.status, action_error=refused.message)
