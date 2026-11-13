"""SQL for the users table and a user's own data (export and deletion)."""

import sqlite3
from typing import Any

from app.db.connection import transaction
from app.domain.models import OsuProfile, User

# Highest session_version ever handed out is below this. New rows start here, so a cookie issued
# for a deleted account can never match the same person's next account.
VERSION_FLOOR_KEY = "session_version_floor"


def get(conn: sqlite3.Connection, osu_id: int) -> User | None:
    row = conn.execute(
        "SELECT osu_id, username, avatar_url, country, timezone, session_version, "
        "created_at, last_login_at FROM users WHERE osu_id = ?",
        (osu_id,),
    ).fetchone()
    return User(**row) if row else None


def record_login(conn: sqlite3.Connection, profile: OsuProfile, now: str) -> User:
    """Create the user or refresh their osu! profile, and stamp the login time."""
    _upsert(conn, profile, now, last_login_at=now)
    return _require(conn, profile.osu_id)


def record_profile(conn: sqlite3.Connection, profile: OsuProfile, now: str) -> User:
    """Create the user or refresh their osu! profile without counting it as a login.

    Used when a captain adds someone who has never logged in, so the roster can show a name.
    """
    _upsert(conn, profile, now, last_login_at=None)
    return _require(conn, profile.osu_id)


def record_if_unknown(conn: sqlite3.Connection, profile: OsuProfile, now: str) -> None:
    """Remember someone seen on a wybin roster so pages can show their name, but never overwrite
    the profile of a person who already logged in."""
    if get(conn, profile.osu_id) is None:
        _upsert(conn, profile, now, last_login_at=None)


def set_timezone(conn: sqlite3.Connection, osu_id: int, timezone: str | None) -> None:
    conn.execute("UPDATE users SET timezone = ? WHERE osu_id = ?", (timezone, osu_id))


def bump_session_version(conn: sqlite3.Connection, osu_id: int) -> None:
    """Invalidate every session cookie issued so far for this user."""
    conn.execute(
        "UPDATE users SET session_version = session_version + 1 WHERE osu_id = ?", (osu_id,)
    )


def export_data(conn: sqlite3.Connection, osu_id: int) -> dict[str, Any]:
    """Everything stored about one user, for the "Download my data" file.

    Session versions, token hashes and other people's ids are left out on purpose.
    """
    profile = conn.execute(
        "SELECT osu_id, username, avatar_url, country, timezone, created_at, last_login_at "
        "FROM users WHERE osu_id = ?",
        (osu_id,),
    ).fetchone()
    memberships = conn.execute(
        "SELECT m.tournament_id, t.name AS tournament, m.role, m.is_substitute, "
        "m.temp_captain_until, m.source "
        "FROM tournament_members m JOIN tournaments t ON t.id = m.tournament_id "
        "WHERE m.osu_id = ? ORDER BY m.tournament_id",
        (osu_id,),
    ).fetchall()
    feed_tokens = conn.execute(
        "SELECT created_at FROM feed_tokens WHERE osu_id = ? ORDER BY created_at", (osu_id,)
    ).fetchall()
    scores = conn.execute(
        "SELECT beatmap_id, gamemode, mods, score, scoring, accuracy, max_combo, misses, passed, "
        "achieved_at, source, source_url, created_at FROM scores WHERE osu_id = ? ORDER BY id",
        (osu_id,),
    ).fetchall()
    notes = conn.execute(
        "SELECT tournament_id, pool_map_id, visibility, body, updated_at FROM notes "
        "WHERE author_osu_id = ? ORDER BY id",
        (osu_id,),
    ).fetchall()
    availability = conn.execute(
        "SELECT tournament_id, slot_start, status FROM availability WHERE osu_id = ? "
        "ORDER BY tournament_id, slot_start",
        (osu_id,),
    ).fetchall()
    return {
        "profile": dict(profile) if profile else None,
        "tournament_memberships": [
            {**dict(row), "is_substitute": bool(row["is_substitute"])} for row in memberships
        ],
        "calendar_feed_tokens": [dict(row) for row in feed_tokens],
        "scores": [{**dict(row), "passed": bool(row["passed"])} for row in scores],
        "notes": [dict(row) for row in notes],
        "availability": [dict(row) for row in availability],
    }


def delete(conn: sqlite3.Connection, osu_id: int) -> None:
    """Remove the user and everything keyed to them. Tournaments they owned stay, ownerless.
    Hand-entered scores and notes of theirs go; scores they typed in for someone else stay but
    forget who typed them.

    The next session_version for new accounts is raised past this one first (see
    VERSION_FLOOR_KEY), so old cookies stay dead after a re-login."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO app_settings (key, value) "
            "SELECT ?, session_version + 1 FROM users WHERE osu_id = ? "
            "ON CONFLICT (key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), "
            "CAST(excluded.value AS INTEGER))",
            (VERSION_FLOOR_KEY, osu_id),
        )
        conn.execute("DELETE FROM tournament_members WHERE osu_id = ?", (osu_id,))
        conn.execute("DELETE FROM notes WHERE author_osu_id = ?", (osu_id,))
        conn.execute("DELETE FROM availability WHERE osu_id = ?", (osu_id,))
        # Scores taken from osu! are public and stay, without the link to who entered them.
        conn.execute(
            "DELETE FROM scores WHERE osu_id = ? AND source IN ('manual', 'import')", (osu_id,)
        )
        conn.execute("UPDATE scores SET entered_by = NULL WHERE entered_by = ?", (osu_id,))
        conn.execute("UPDATE lineups SET updated_by = NULL WHERE updated_by = ?", (osu_id,))
        conn.execute("UPDATE tournament_members SET added_by = NULL WHERE added_by = ?", (osu_id,))
        conn.execute("DELETE FROM users WHERE osu_id = ?", (osu_id,))  # cascades feed_tokens


def _upsert(
    conn: sqlite3.Connection, profile: OsuProfile, now: str, last_login_at: str | None
) -> None:
    conn.execute(
        "INSERT INTO users "
        "(osu_id, username, avatar_url, country, created_at, last_login_at, session_version) "
        "VALUES (?, ?, ?, ?, ?, ?, COALESCE("
        "(SELECT CAST(value AS INTEGER) FROM app_settings WHERE key = ?), 1)) "
        "ON CONFLICT (osu_id) DO UPDATE SET username = excluded.username, "
        "avatar_url = excluded.avatar_url, country = excluded.country, "
        "last_login_at = COALESCE(excluded.last_login_at, users.last_login_at)",
        (
            profile.osu_id,
            profile.username,
            profile.avatar_url,
            profile.country,
            now,
            last_login_at,
            VERSION_FLOOR_KEY,
        ),
    )


def _require(conn: sqlite3.Connection, osu_id: int) -> User:
    user = get(conn, osu_id)
    if user is None:
        raise LookupError(f"user {osu_id} vanished after upsert")
    return user
