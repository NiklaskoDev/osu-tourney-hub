"""Remembers wybin tournaments an admin deleted, so sign-up tracking does not import them again.

Kept as one JSON value in `app_settings` (no table of its own): `{"ids": [...], "slugs": [...]}`.
"""

import json
import sqlite3

KEY = "deleted_wybin_tournaments"
MAX_REMEMBERED = 5000  # far above any real use; keeps the value from growing without limit


def is_deleted(conn: sqlite3.Connection, wybin_id: int | None, slug: str | None) -> bool:
    ids, slugs = _load(conn)
    return (wybin_id is not None and wybin_id in ids) or (slug is not None and slug in slugs)


def remember(conn: sqlite3.Connection, wybin_id: int | None, slug: str | None) -> None:
    ids, slugs = _load(conn)
    if wybin_id is not None and wybin_id not in ids:
        ids.append(wybin_id)
    if slug is not None and slug not in slugs:
        slugs.append(slug)
    _save(conn, ids, slugs)


def forget(conn: sqlite3.Connection, wybin_id: int | None, slug: str | None) -> None:
    """An admin imports the tournament again on purpose: tracking may see it as known again."""
    ids, slugs = _load(conn)
    new_ids = [item for item in ids if item != wybin_id]
    new_slugs = [item for item in slugs if item != slug]
    if (new_ids, new_slugs) != (ids, slugs):
        _save(conn, new_ids, new_slugs)


def _load(conn: sqlite3.Connection) -> tuple[list[int], list[str]]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    try:
        data = json.loads(row["value"]) if row else {}
        ids = [item for item in data.get("ids", []) if isinstance(item, int)]
        slugs = [item for item in data.get("slugs", []) if isinstance(item, str)]
    except (ValueError, AttributeError, TypeError):
        return [], []  # a damaged value must not stop the app: nothing is remembered then
    return ids, slugs


def _save(conn: sqlite3.Connection, ids: list[int], slugs: list[str]) -> None:
    value = json.dumps({"ids": ids[-MAX_REMEMBERED:], "slugs": slugs[-MAX_REMEMBERED:]})
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (KEY, value),
    )
