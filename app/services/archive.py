"""Archive, hide and restore tournaments. An archived tournament is read-only for everyone
(`require_writable`); a hidden one is invisible to non-admins and never re-imported."""

import sqlite3
from datetime import datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import Tournament
from app.repos import deleted_tournaments as deleted_repo
from app.repos import tournaments as tournaments_repo

MAX_PLACING_LENGTH = 60


class ArchiveError(Exception):
    """A change the rules refuse. `status` is the HTTP status the web layer answers with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def clean_placing(text: str) -> str | None:
    """Final placing as typed ("Top 8", "Champions"); empty means none."""
    value = text.strip()
    if len(value) > MAX_PLACING_LENGTH:
        raise ArchiveError(f"Keep the final placing under {MAX_PLACING_LENGTH} characters.", 422)
    return value or None


def archive(
    conn: sqlite3.Connection, tournament_id: int, placing: str, now: datetime
) -> Tournament:
    cleaned = clean_placing(placing)
    with transaction(conn):
        tournament = _require(conn, tournament_id)
        if tournament.status == "archived":
            raise ArchiveError("This tournament is already archived.", 409)
        if tournament.status == "hidden":
            raise ArchiveError("This tournament is hidden. Restore it before archiving it.", 409)
        tournaments_repo.set_status(conn, tournament_id, "archived", to_utc_iso(now))
        if cleaned is not None:
            tournaments_repo.set_final_placing(conn, tournament_id, cleaned)
    return _require(conn, tournament_id)


def restore(conn: sqlite3.Connection, tournament_id: int) -> Tournament:
    """Make an archived or hidden tournament active again. Its final placing is kept."""
    with transaction(conn):
        tournament = _require(conn, tournament_id)
        if tournament.status == "active":
            raise ArchiveError("This tournament is already active.", 409)
        tournaments_repo.set_status(conn, tournament_id, "active", None)
    return _require(conn, tournament_id)


def hide(conn: sqlite3.Connection, tournament_id: int) -> Tournament:
    with transaction(conn):
        tournament = _require(conn, tournament_id)
        if tournament.status == "hidden":
            raise ArchiveError("This tournament is already hidden.", 409)
        if tournament.status == "archived":
            raise ArchiveError("This tournament is archived. Restore it before hiding it.", 409)
        tournaments_repo.set_status(conn, tournament_id, "hidden", None)
    return _require(conn, tournament_id)


def delete_tournament(conn: sqlite3.Connection, tournament_id: int) -> Tournament:
    """Delete a tournament with everything in it. A wybin tournament is remembered, so sign-up
    tracking does not bring it back; an admin can still import it by hand."""
    with transaction(conn):
        tournament = _require(conn, tournament_id)
        if tournament.wybin_id is not None or tournament.slug:
            deleted_repo.remember(conn, tournament.wybin_id, tournament.slug)
        tournaments_repo.delete(conn, tournament_id)
    return tournament


def _require(conn: sqlite3.Connection, tournament_id: int) -> Tournament:
    tournament = tournaments_repo.get(conn, tournament_id)
    if tournament is None:
        raise ArchiveError("This tournament does not exist.", 404)
    return tournament
