"""SQL for calendar feed tokens. Only the SHA-256 of a token is stored."""

import sqlite3


def replace(conn: sqlite3.Connection, osu_id: int, token_hash: str, now: str) -> None:
    """Make `token_hash` the person's only token; an earlier link stops working."""
    conn.execute("DELETE FROM feed_tokens WHERE osu_id = ?", (osu_id,))
    conn.execute(
        "INSERT INTO feed_tokens (token_hash, osu_id, created_at) VALUES (?, ?, ?)",
        (token_hash, osu_id, now),
    )


def revoke(conn: sqlite3.Connection, osu_id: int) -> None:
    conn.execute("DELETE FROM feed_tokens WHERE osu_id = ?", (osu_id,))


def created_at(conn: sqlite3.Connection, osu_id: int) -> str | None:
    """When the person's current link was made, or None when they have none."""
    row = conn.execute("SELECT created_at FROM feed_tokens WHERE osu_id = ?", (osu_id,)).fetchone()
    return row["created_at"] if row else None


def find_user(conn: sqlite3.Connection, token_hash: str) -> int | None:
    row = conn.execute(
        "SELECT osu_id FROM feed_tokens WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    return int(row["osu_id"]) if row else None
