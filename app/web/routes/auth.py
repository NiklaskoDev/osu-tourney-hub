"""Login with osu! (OAuth2 authorization code flow), callback and logout."""

import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from app.clients.osu import OsuAuthError, OsuClient, OsuError
from app.clock import Clock, to_utc_iso
from app.repos import users as users_repo
from app.web.deps import get_clock, get_db, get_osu
from app.web.error_pages import error_page
from app.web.security import limit_auth_requests
from app.web.session import NEXT_KEY, establish_session, safe_next

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", dependencies=[Depends(limit_auth_requests)])

LOGIN_FAILED = "The osu! login did not complete. Start again from the login button."
OSU_DOWN = "osu! did not answer. Try again in a few minutes."


@router.get("/login")
async def login(request: Request, next: str = "/", osu: OsuClient = Depends(get_osu)) -> Response:
    """Remember where to go afterwards (relative paths only) and send the user to osu!."""
    request.session[NEXT_KEY] = safe_next(next)
    return await osu.authorize_redirect(request)


@router.get("/callback")
async def callback(
    request: Request,
    osu: OsuClient = Depends(get_osu),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    destination = safe_next(request.session.get(NEXT_KEY))
    try:
        profile = await osu.complete_login(request)
    except OsuAuthError:
        return error_page(request, 400, LOGIN_FAILED)
    except OsuError:
        return error_page(request, 502, OSU_DOWN)
    now = clock.now()
    user = users_repo.record_login(db, profile, to_utc_iso(now))
    establish_session(request, user, now)
    logger.info("Login of osu! user %s", user.osu_id)
    return RedirectResponse(destination, status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/", status_code=303)
