"""Who may do what in a tournament. Pure functions: the caller passes in the current time."""

from datetime import datetime

from app.clock import from_utc_iso
from app.domain.models import Member, Role

ROLE_LABELS = {
    "captain": "Captain",
    "co_captain": "Co-captain",
    "player": "Player",
    "guest": "Guest",
}
ADMIN_LABEL = "Admin"


def is_temp_captain_active(member: Member, now: datetime) -> bool:
    """A player with a future `temp_captain_until` acts as captain; expiry is judged per request."""
    if member.role != "player" or not member.temp_captain_until:
        return False
    try:
        return from_utc_iso(member.temp_captain_until) > now
    except ValueError:
        return False  # a malformed value must never grant access


def effective_role(member: Member, now: datetime) -> Role:
    if member.role in ("captain", "co_captain"):
        return Role.CAPTAIN
    if member.role == "player":
        return Role.CAPTAIN if is_temp_captain_active(member, now) else Role.PLAYER
    return Role.GUEST


def role_for(is_admin: bool, member: Member | None, now: datetime) -> Role | None:
    """The viewer's level in a tournament, or None when they have no access at all."""
    if is_admin:
        return Role.ADMIN
    if member is None:
        return None
    return effective_role(member, now)


def role_label(member: Member | None) -> str:
    """Name shown for someone: their stored role, or Admin for an admin outside the roster."""
    return ADMIN_LABEL if member is None else ROLE_LABELS[member.role]
