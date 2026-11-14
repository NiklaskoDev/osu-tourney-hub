"""SQL for the lobby scanner's bookkeeping: open lobbies, skipped ranges and per-minute counters."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenRow:
    mp_match_id: int
    first_seen_at: str
    last_event_id: int | None
    events_fetched: int
    next_check_at: str


@dataclass(frozen=True)
class Gap:
    id: int
    start_id: int
    end_id: int
    reason: str
    created_at: str
    backfilled_until: int | None

    @property
    def backfill_requested(self) -> bool:
        return self.backfilled_until is not None

    @property
    def backfill_done(self) -> bool:
        return self.backfilled_until is not None and self.backfilled_until >= self.end_id

    @property
    def size(self) -> int:
        return self.end_id - self.start_id + 1


@dataclass(frozen=True)
class StatRow:
    minute: str
    ids_scanned: int
    not_found: int
    matches_with_hits: int
    requests: int
    head_id: int | None
    errors: int


# --- open lobbies ---------------------------------------------------------------------------


def upsert_open(
    conn: sqlite3.Connection,
    mp_match_id: int,
    now: str,
    last_event_id: int | None,
    events_fetched: int,
    next_check_at: str,
) -> None:
    """Track an open lobby. Seen again, it keeps its first-seen time and only moves forward."""
    conn.execute(
        "INSERT INTO scan_open (mp_match_id, first_seen_at, last_event_id, events_fetched, "
        "next_check_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (mp_match_id) DO UPDATE SET "
        "last_event_id = CASE WHEN excluded.last_event_id IS NULL THEN scan_open.last_event_id "
        "WHEN scan_open.last_event_id IS NULL THEN excluded.last_event_id "
        "ELSE MAX(scan_open.last_event_id, excluded.last_event_id) END, "
        "events_fetched = scan_open.events_fetched + excluded.events_fetched, "
        "next_check_at = excluded.next_check_at",
        (mp_match_id, now, last_event_id, events_fetched, next_check_at),
    )


def list_open(conn: sqlite3.Connection) -> list[OpenRow]:
    rows = conn.execute(
        "SELECT mp_match_id, first_seen_at, last_event_id, events_fetched, next_check_at "
        "FROM scan_open ORDER BY mp_match_id"
    ).fetchall()
    return [OpenRow(**row) for row in rows]


def delete_open(conn: sqlite3.Connection, mp_match_id: int) -> None:
    conn.execute("DELETE FROM scan_open WHERE mp_match_id = ?", (mp_match_id,))


def count_open(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM scan_open").fetchone()["n"])


# --- skipped ranges -------------------------------------------------------------------------


def insert_gap(conn: sqlite3.Connection, start_id: int, end_id: int, reason: str, now: str) -> int:
    cursor = conn.execute(
        "INSERT INTO scan_gaps (start_id, end_id, reason, created_at) VALUES (?, ?, ?, ?)",
        (start_id, end_id, reason, now),
    )
    return int(cursor.lastrowid)


def record_fetch_error(conn: sqlite3.Connection, match_id: int, now: str) -> None:
    """Remember an id osu! kept failing for as a skipped range, so it can be backfilled. An id
    right after an existing 'fetch_error' range extends it."""
    cursor = conn.execute(
        "UPDATE scan_gaps SET end_id = ? WHERE reason = 'fetch_error' AND end_id = ?",
        (match_id, match_id - 1),
    )
    if cursor.rowcount == 0:
        insert_gap(conn, match_id, match_id, "fetch_error", now)


def list_gaps(conn: sqlite3.Connection) -> list[Gap]:
    rows = conn.execute(
        "SELECT id, start_id, end_id, reason, created_at, backfilled_until FROM scan_gaps "
        "ORDER BY start_id, id"
    ).fetchall()
    return [Gap(**row) for row in rows]


def get_gap(conn: sqlite3.Connection, gap_id: int) -> Gap | None:
    row = conn.execute(
        "SELECT id, start_id, end_id, reason, created_at, backfilled_until FROM scan_gaps "
        "WHERE id = ?",
        (gap_id,),
    ).fetchone()
    return Gap(**row) if row else None


def request_backfill(conn: sqlite3.Connection, gap_id: int) -> bool:
    """Mark a gap for backfilling. Returns False when it was already requested (or is unknown)."""
    cursor = conn.execute(
        "UPDATE scan_gaps SET backfilled_until = start_id - 1 "
        "WHERE id = ? AND backfilled_until IS NULL",
        (gap_id,),
    )
    return cursor.rowcount == 1


def next_backfill(conn: sqlite3.Connection) -> Gap | None:
    """The oldest gap that was asked for and is not finished."""
    row = conn.execute(
        "SELECT id, start_id, end_id, reason, created_at, backfilled_until FROM scan_gaps "
        "WHERE backfilled_until IS NOT NULL AND backfilled_until < end_id "
        "ORDER BY start_id, id LIMIT 1"
    ).fetchone()
    return Gap(**row) if row else None


def delete_gaps(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.executemany("DELETE FROM scan_gaps WHERE id = ?", [(gap_id,) for gap_id in ids])


def stop_backfill(conn: sqlite3.Connection, gap_id: int) -> bool:
    """Stop a requested, unfinished backfill and keep what was read: the ids already read leave the
    range, the rest stays listed (and can be backfilled again). Returns whether a row changed."""
    cursor = conn.execute(
        "UPDATE scan_gaps SET start_id = backfilled_until + 1, backfilled_until = NULL "
        "WHERE id = ? AND backfilled_until IS NOT NULL AND backfilled_until < end_id",
        (gap_id,),
    )
    return cursor.rowcount == 1


def set_backfilled_until(conn: sqlite3.Connection, gap_id: int, last_id: int) -> None:
    """Record backfill progress. Only while the backfill is still requested and only forwards: a
    worker that finished an id after an admin stopped the backfill must not restart it."""
    conn.execute(
        "UPDATE scan_gaps SET backfilled_until = ? "
        "WHERE id = ? AND backfilled_until IS NOT NULL AND backfilled_until < ?",
        (last_id, gap_id, last_id),
    )


# --- per-minute counters --------------------------------------------------------------------


def add_stats(
    conn: sqlite3.Connection,
    minute: str,
    ids_scanned: int = 0,
    not_found: int = 0,
    matches_with_hits: int = 0,
    requests: int = 0,
    head_id: int | None = None,
    errors: int = 0,
) -> None:
    """Add to the counters of one minute. `head_id` keeps the highest value seen that minute."""
    conn.execute(
        "INSERT INTO scan_stats (minute, ids_scanned, not_found, matches_with_hits, requests, "
        "head_id, errors) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (minute) DO UPDATE SET "
        "ids_scanned = scan_stats.ids_scanned + excluded.ids_scanned, "
        "not_found = scan_stats.not_found + excluded.not_found, "
        "matches_with_hits = scan_stats.matches_with_hits + excluded.matches_with_hits, "
        "requests = scan_stats.requests + excluded.requests, "
        "errors = scan_stats.errors + excluded.errors, "
        "head_id = CASE WHEN excluded.head_id IS NULL THEN scan_stats.head_id "
        "WHEN scan_stats.head_id IS NULL THEN excluded.head_id "
        "ELSE MAX(scan_stats.head_id, excluded.head_id) END",
        (minute, ids_scanned, not_found, matches_with_hits, requests, head_id, errors),
    )


def stats_since(conn: sqlite3.Connection, first_minute: str) -> list[StatRow]:
    rows = conn.execute(
        "SELECT minute, ids_scanned, not_found, matches_with_hits, requests, head_id, errors "
        "FROM scan_stats WHERE minute >= ? ORDER BY minute",
        (first_minute,),
    ).fetchall()
    return [StatRow(**row) for row in rows]


def prune_stats(conn: sqlite3.Connection, before_minute: str) -> int:
    cursor = conn.execute("DELETE FROM scan_stats WHERE minute < ?", (before_minute,))
    return cursor.rowcount
