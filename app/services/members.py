"""Roster changes and the rules that protect them. Raises `MemberError` for a refused change."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.clock import to_utc_iso
from app.db.connection import transaction
from app.domain.models import MEMBER_ROLES, Member, OsuProfile
from app.repos import members as members_repo
from app.repos import users as users_repo

FORM_TIME_LENGTH = len("YYYY-MM-DDTHH:MM")


class MemberError(Exception):
    """A change the rules refuse. `status` is the HTTP status the web layer answers with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Actor:
    osu_id: int
    is_admin: bool


def parse_temp_captain_until(text: str) -> str | None:
    """Turn the form's `YYYY-MM-DDTHH:MM` (read as UTC) into the stored format. Empty = none."""
    value = text.strip()
    if not value:
        return None
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    except ValueError as error:
        raise MemberError("Enter the end of the captain period as a date and time.", 422) from error
    return to_utc_iso(moment)


def form_time(stored: str | None) -> str:
    """Stored timestamp -> the value of a `datetime-local` input (minute precision, UTC)."""
    return stored[:FORM_TIME_LENGTH] if stored else ""


def add_guest(
    conn: sqlite3.Connection, tournament_id: int, actor: Actor, profile: OsuProfile, now: datetime
) -> None:
    with transaction(conn):
        if members_repo.get(conn, tournament_id, profile.osu_id):
            raise MemberError(f"{profile.username} is already a member of this tournament.", 409)
        users_repo.record_profile(conn, profile, to_utc_iso(now))
        members_repo.add(conn, tournament_id, profile.osu_id, "guest", "manual", actor.osu_id)


def update_member(
    conn: sqlite3.Connection,
    tournament_id: int,
    actor: Actor,
    target_id: int,
    role: str,
    is_substitute: bool,
    temp_captain_text: str,
) -> Member:
    """Apply a role, substitute and temporary-captain change to one roster entry."""
    if role not in MEMBER_ROLES:
        raise MemberError("Choose one of the listed roles.", 422)
    temp_captain_until = parse_temp_captain_until(temp_captain_text)
    with transaction(conn):
        target = members_repo.get(conn, tournament_id, target_id)
        if target is None:
            raise MemberError("This person is not on the roster.", 404)
        # Only players can be temporary captains and guests are never substitutes.
        if role != "player":
            temp_captain_until = None
        if role == "guest":
            is_substitute = False
        _refuse_self_promotion(actor, target, role, temp_captain_until)
        if target.role == "captain" and role != "captain":
            _require_another_captain(conn, tournament_id)
        members_repo.update(conn, tournament_id, target_id, role, is_substitute, temp_captain_until)
    updated = members_repo.get(conn, tournament_id, target_id)
    if updated is None:
        raise LookupError("member vanished after update")
    return updated


def remove_member(
    conn: sqlite3.Connection, tournament_id: int, actor: Actor, target_id: int
) -> Member:
    with transaction(conn):
        target = members_repo.get(conn, tournament_id, target_id)
        if target is None:
            raise MemberError("This person is not on the roster.", 404)
        if target.role == "captain":
            _require_another_captain(conn, tournament_id)
        members_repo.remove(conn, tournament_id, target_id)
    return target


def _refuse_self_promotion(
    actor: Actor, target: Member, new_role: str, new_temp_captain_until: str | None
) -> None:
    """Nobody but an admin changes their own role or captain period, so a temporary captain
    cannot extend their own term and a co-captain cannot make themselves captain."""
    if actor.is_admin or actor.osu_id != target.osu_id:
        return
    if new_role != target.role or form_time(new_temp_captain_until) != form_time(
        target.temp_captain_until
    ):
        raise MemberError("You cannot change your own role. Ask another captain.", 403)


def _require_another_captain(conn: sqlite3.Connection, tournament_id: int) -> None:
    if members_repo.count_with_role(conn, tournament_id, "captain") <= 1:
        raise MemberError(
            "A tournament needs at least one captain. Make someone else captain first.", 409
        )
