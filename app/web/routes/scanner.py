"""Admin page for the lobby scanner: status, pause and resume, starting from a chosen lobby id, and
backfilling skipped ranges."""

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.clock import Clock
from app.config import Settings
from app.domain.models import User
from app.repos import scan as scan_repo
from app.services import links
from app.services.skimmer import (
    ENTER_AN_ID,
    ScannerStatus,
    Skimmer,
    StartError,
    StartPlan,
    backfill_eta_minutes,
    plan_start,
    scanner_status,
    start_from,
)
from app.web.deps import get_clock, get_db, get_settings, require_admin
from app.web.errors import UserFacingError
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/admin/scanner")

GAP_REASONS = {
    "skipped_backlog": "Scanner fell behind",
    "fetch_error": "osu! kept failing",
    "admin_skip": "Chosen by an admin",
}
FALLING_BEHIND = (
    "The scanner is falling behind. Raise SCANNER_BUDGET_PER_MIN or expect skipped ranges."
)
NOT_SET = "-"
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * 60
SWITCHED_OFF = "The scanner is switched off (SCANNER_ENABLED is false)."


def _per_min(short: float | None, long: float | None) -> str:
    def one(value: float | None) -> str:
        return NOT_SET if value is None else f"{value:.1f}"

    return f"{one(short)} over 15 minutes, {one(long)} over 1 hour"


def _state_text(status: ScannerStatus) -> str:
    if not status.enabled:
        return "Switched off (SCANNER_ENABLED is false)"
    return "Paused" if status.paused else "Running"


def _eta_text(minutes: float | None) -> str:
    if minutes is None:
        return NOT_SET
    if minutes < 1:
        return "less than a minute"
    if minutes < MINUTES_PER_HOUR:
        return f"about {round(minutes)} minutes"
    if minutes < MINUTES_PER_DAY:
        return f"about {round(minutes / MINUTES_PER_HOUR)} hours"
    return f"about {round(minutes / MINUTES_PER_DAY)} days"


def status_rows(status: ScannerStatus) -> list[tuple[str, str]]:
    """The status as label and text pairs, in the order the page shows them."""
    rate = status.not_found_rate_60
    return [
        ("State", _state_text(status)),
        ("Scanned up to lobby id", NOT_SET if status.cursor_id is None else str(status.cursor_id)),
        ("Newest lobby id", NOT_SET if status.head_id is None else str(status.head_id)),
        ("Lobby ids still to scan", NOT_SET if status.backlog is None else f"{status.backlog:,}"),
        (
            "New lobby ids per minute",
            _per_min(status.created_per_min_15, status.created_per_min_60),
        ),
        ("Ids scanned per minute", _per_min(status.scanned_per_min_15, status.scanned_per_min_60)),
        (
            f"osu! requests per minute (budget {status.budget_per_min})",
            _per_min(status.requests_per_min_15, status.requests_per_min_60),
        ),
        ("Deleted or private ids, last hour", NOT_SET if rate is None else f"{rate:.0%}"),
        ("Ids given up on after errors, last hour", str(status.errors_60)),
        ("Open lobbies waiting to finish", str(status.open_lobbies)),
        ("Time to catch up", _eta_text(status.eta_minutes)),
        ("Last error", status.last_error or "None"),
    ]


def _context(
    db: sqlite3.Connection, settings: Settings, clock: Clock, **extra: Any
) -> dict[str, Any]:
    status = scanner_status(db, settings, clock.now())
    return {
        "status": status,
        "status_rows": status_rows(status),
        "falling_behind_text": FALLING_BEHIND,
        "gap_reasons": GAP_REASONS,
        **extra,
    }


@router.get("", response_class=HTMLResponse)
async def scanner_page(
    request: Request,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    context = _context(db, settings, clock, nav_current="admin")
    return render(request, "pages/admin/scanner.html", context)


@router.get("/status", response_class=HTMLResponse)
async def scanner_status_partial(
    request: Request,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    clock: Clock = Depends(get_clock),
) -> HTMLResponse:
    """The part of the page that refreshes itself every 15 seconds."""
    return render(request, "partials/scanner_status.html", _context(db, settings, clock))


@router.post("/pause")
async def pause(
    request: Request,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    Skimmer.set_paused(db, True)
    flash(request, "success", "The scanner is paused. It carries on from where it stopped.")
    return RedirectResponse("/admin/scanner", status_code=303)


@router.post("/resume")
async def resume(
    request: Request,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    Skimmer.set_paused(db, False)
    flash(request, "success", "The scanner is running again.")
    return RedirectResponse("/admin/scanner", status_code=303)


@router.post("/gaps/{gap_id}/backfill")
async def backfill_gap(
    request: Request,
    gap_id: int,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    if scan_repo.request_backfill(db, gap_id):
        flash(
            request,
            "success",
            "Backfill started. It only runs while the scanner is nearly caught up.",
        )
    elif scan_repo.get_gap(db, gap_id) is None:
        raise UserFacingError(404, "This skipped range does not exist.")
    else:
        flash(request, "info", "This range is already being backfilled.")
    return RedirectResponse("/admin/scanner", status_code=303)


def _start_page(
    request: Request,
    db: sqlite3.Connection,
    settings: Settings,
    clock: Clock,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    context = _context(db, settings, clock, nav_current="admin", **extra)
    return render(request, "pages/admin/scanner.html", context, status_code=status_code)


@router.post("/start")
async def start_scanner(
    request: Request,
    start: str = Form(""),
    confirmed: str = Form(""),
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Start lobby scanning from a lobby id or an osu! match link: an older id is read in the
    background, a newer one skips ahead."""
    entered = start.strip()
    start_id = links.match_id(entered)

    def refused(message: str) -> HTMLResponse:
        return _start_page(
            request, db, settings, clock, 422, start_entered=entered, start_error=message
        )

    if start_id is None:
        return refused(ENTER_AN_ID)
    if not settings.scanner_enabled:
        raise UserFacingError(409, SWITCHED_OFF)
    status = scanner_status(db, settings, clock.now())
    try:
        live = request.app.state.skimmer.live_cursor
        cursor = status.cursor_id if live is None else live
        plan = plan_start(start_id, cursor, status.head_id, status.gaps)
    except StartError as error:
        return refused(str(error))
    if plan.kind == "backfill" and plan.size > settings.scanner_max_backlog and confirmed != "1":
        eta = backfill_eta_minutes(plan.size, status)
        return _start_page(
            request,
            db,
            settings,
            clock,
            start_entered=entered,
            pending_start=plan,
            pending_eta_text=None if eta is None else _eta_text(eta),
        )
    try:
        start_from(db, request.app.state.skimmer, plan, clock.now())
    except StartError as error:
        return refused(str(error))
    kind, text = _start_flash(plan)
    flash(request, kind, text)
    return RedirectResponse("/admin/scanner", status_code=303)


def _start_flash(plan: StartPlan) -> tuple[str, str]:
    if plan.kind == "backfill":
        return "success", (
            f"Lobby ids {plan.start_id:,} to {plan.end_id:,} will be read in the background "
            "with spare budget, oldest first."
        )
    if plan.kind == "skip":
        return "success", (
            f"The scanner now continues from lobby id {plan.end_id + 1:,}. Ids "
            f"{plan.start_id:,} to {plan.end_id:,} are listed under Skipped ranges and can be "
            "backfilled."
        )
    return "info", "The scanner already continues from this lobby id."


@router.post("/gaps/{gap_id}/stop")
async def stop_gap(
    request: Request,
    gap_id: int,
    _admin: User = Depends(require_admin),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    if scan_repo.stop_backfill(db, gap_id):
        flash(
            request,
            "success",
            "Backfill stopped. What was read stays; the rest can be backfilled again.",
        )
    elif scan_repo.get_gap(db, gap_id) is None:
        raise UserFacingError(404, "This skipped range does not exist.")
    else:
        flash(request, "info", "This range is not being backfilled.")
    return RedirectResponse("/admin/scanner", status_code=303)
