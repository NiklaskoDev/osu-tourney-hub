"""SQL for admin_wybin_links: which wybin account belongs to which admin, for sign-up tracking."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class WybinLink:
    osu_id: int
    wybin_user_id: int
    last_checked_at: str | None


def get(conn: sqlite3.Connection, osu_id: int) -> WybinLink | None:
    row = conn.execute(
        "SELECT osu_id, wybin_user_id, last_checked_at FROM admin_wybin_links WHERE osu_id = ?",
        (osu_id,),
    ).fetchone()
    return WybinLink(**row) if row else None


def list_all(conn: sqlite3.Connection) -> list[WybinLink]:
    rows = conn.execute(
        "SELECT osu_id, wybin_user_id, last_checked_at FROM admin_wybin_links ORDER BY osu_id"
    ).fetchall()
    return [WybinLink(**row) for row in rows]


def upsert(conn: sqlite3.Connection, osu_id: int, wybin_user_id: int) -> None:
    """Link or re-link an admin. Raises `sqlite3.IntegrityError` when another admin has the id."""
    conn.execute(
        "INSERT INTO admin_wybin_links (osu_id, wybin_user_id) VALUES (?, ?) "
        "ON CONFLICT (osu_id) DO UPDATE SET wybin_user_id = excluded.wybin_user_id, "
        "last_checked_at = NULL",
        (osu_id, wybin_user_id),
    )


def remove(conn: sqlite3.Connection, osu_id: int) -> None:
    conn.execute("DELETE FROM admin_wybin_links WHERE osu_id = ?", (osu_id,))


def mark_checked(conn: sqlite3.Connection, osu_id: int, now: str) -> None:
    conn.execute("UPDATE admin_wybin_links SET last_checked_at = ? WHERE osu_id = ?", (now, osu_id))
