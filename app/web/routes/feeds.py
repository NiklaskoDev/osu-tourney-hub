"""The calendar feed: an .ics file at an unguessable address, for apps that cannot log in."""

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.repos import feeds as feeds_repo
from app.services import feed_tokens, schedule
from app.services.ical import build_calendar
from app.web.deps import get_db
from app.web.errors import UserFacingError

router = APIRouter()

CALENDAR_NAME = "osu! tourney hub matches"
NOT_FOUND = "This calendar link does not exist or was revoked."


@router.get("/feeds/{token}.ics")
async def calendar_feed(token: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Matches of the token holder's tournaments. No login: the token is the credential, and a
    wrong or revoked one is a plain 404."""
    osu_id = feeds_repo.find_user(db, feed_tokens.hash_token(token))
    if osu_id is None:
        raise UserFacingError(404, NOT_FOUND)
    calendar = build_calendar(schedule.feed_events(db, osu_id), CALENDAR_NAME)
    return Response(
        calendar,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, no-store"},
    )
