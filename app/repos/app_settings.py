"""SQL for app_settings: small named values that are not tied to a table of their own."""

import sqlite3


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get_prefixed(conn: sqlite3.Connection, prefix: str) -> dict[str, str]:
    """Every value whose key starts with `prefix`, by key."""
    rows = conn.execute(
        "SELECT key, value FROM app_settings WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
