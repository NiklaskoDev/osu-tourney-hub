"""The signed-in user's own page: timezone, sessions, data export and deletion."""

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.clock import Clock
from app.domain.models import User
from app.domain.timezones import is_valid_timezone, timezone_names
from app.repos import users as users_repo
from app.web.deps import get_clock, get_db, require_login
from app.web.session import flash, has_recent_login
from app.web.templating import render

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/me")

EXPORT_FILENAME = "osu-tourney-hub-my-data.json"
DELETED_MESSAGE = "Your data was deleted."


def _page(
    request: Request, user: User, clock: Clock, timezone_error: str | None = None, **extra: Any
) -> HTMLResponse:
    context = {
        "me": user,
        "timezone_options": [("", "Choose your timezone"), *[(n, n) for n in timezone_names()]],
        "timezone_error": timezone_error,
        "recent_login": has_recent_login(request, clock.now()),
        **extra,
    }
    return render(
        request,
        "pages/me.html",
        context,
        status_code=422 if timezone_error else 200,
    )


@router.get("", response_class=HTMLResponse)
async def me_page(
    request: Request, user: User = Depends(require_login), clock: Clock = Depends(get_clock)
) -> HTMLResponse:
    return _page(request, user, clock)


@router.post("/timezone")
async def save_timezone(
    request: Request,
    timezone: str = Form(""),
    user: User = Depends(require_login),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    value = timezone.strip()
    if not is_valid_timezone(value):
        return _page(
            request,
            user,
            clock,
            timezone_error="Choose a timezone from the list.",
        )
    users_repo.set_timezone(db, user.osu_id, value)
    flash(request, "success", f"Timezone saved: {value}.")
    return RedirectResponse("/me", status_code=303)


@router.get("/export")
async def export_data(
    user: User = Depends(require_login), db: sqlite3.Connection = Depends(get_db)
) -> JSONResponse:
    return JSONResponse(
        users_repo.export_data(db, user.osu_id),
        headers={
            "Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/logout-all")
async def logout_everywhere(
    request: Request, user: User = Depends(require_login), db: sqlite3.Connection = Depends(get_db)
) -> Response:
    """Bump the session version so every cookie issued so far stops working, this one included."""
    users_repo.bump_session_version(db, user.osu_id)
    request.session.clear()
    logger.info("Sessions of osu! user %s invalidated", user.osu_id)
    return RedirectResponse("/", status_code=303)


@router.post("/delete")
async def delete_my_data(
    request: Request,
    user: User = Depends(require_login),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Deletion needs a completed osu! login in the last five minutes, else the user is sent
    through login again and returns to the profile page."""
    if not has_recent_login(request, clock.now()):
        return RedirectResponse("/auth/login?next=/me", status_code=303)
    users_repo.delete(db, user.osu_id)
    request.session.clear()
    flash(request, "success", DELETED_MESSAGE)
    logger.info("Data of osu! user %s deleted", user.osu_id)
    return RedirectResponse("/", status_code=303)
