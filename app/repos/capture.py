"""SQL for the score capture queue and the log of finished pulls."""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

Pair = tuple[int, int]  # (osu_id, beatmap_id)


@dataclass(frozen=True)
class QueueItem:
    osu_id: int
    beatmap_id: int
    gamemode: int
    priority: int
    attempts: int


def enqueue(
    conn: sqlite3.Connection, pairs: Collection[Pair], gamemode: int, priority: int, now: str
) -> None:
    """Queue pulls. A pair that is already waiting keeps the more urgent of the two priorities."""
    conn.executemany(
        "INSERT INTO capture_queue (osu_id, beatmap_id, gamemode, priority, requested_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (osu_id, beatmap_id, gamemode) DO UPDATE SET "
        "priority = MIN(priority, excluded.priority)",
        [(osu_id, beatmap_id, gamemode, priority, now) for osu_id, beatmap_id in pairs],
    )


def pulled_since(
    conn: sqlite3.Connection,
    pairs: Collection[Pair],
    gamemode: int,
    cutoff: str,
) -> set[Pair]:
    """The pairs that were last pulled at or after `cutoff`."""
    if not pairs:
        return set()
    players = sorted({osu_id for osu_id, _ in pairs})
    beatmaps = sorted({beatmap_id for _, beatmap_id in pairs})
    rows = conn.execute(
        "SELECT osu_id, beatmap_id FROM capture_log WHERE gamemode = ? AND last_pulled_at >= ? "  # noqa: S608
        f"AND osu_id IN ({_marks(players)}) AND beatmap_id IN ({_marks(beatmaps)})",
        [gamemode, cutoff, *players, *beatmaps],
    ).fetchall()
    return {(row["osu_id"], row["beatmap_id"]) for row in rows} & set(pairs)


def next_batch(conn: sqlite3.Connection, limit: int) -> list[QueueItem]:
    """The most urgent waiting pulls, oldest request first inside a priority."""
    rows = conn.execute(
        "SELECT osu_id, beatmap_id, gamemode, priority, attempts FROM capture_queue "
        "ORDER BY priority, requested_at, rowid LIMIT ?",
        (limit,),
    ).fetchall()
    return [QueueItem(**row) for row in rows]


def mark_pulled(conn: sqlite3.Connection, item: QueueItem, now: str) -> None:
    """The pull finished: take it off the queue and remember when."""
    conn.execute(
        "DELETE FROM capture_queue WHERE osu_id = ? AND beatmap_id = ? AND gamemode = ?",
        (item.osu_id, item.beatmap_id, item.gamemode),
    )
    conn.execute(
        "INSERT INTO capture_log (osu_id, beatmap_id, gamemode, last_pulled_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (osu_id, beatmap_id, gamemode) "
        "DO UPDATE SET last_pulled_at = excluded.last_pulled_at",
        (item.osu_id, item.beatmap_id, item.gamemode, now),
    )


def record_failure(conn: sqlite3.Connection, item: QueueItem, max_attempts: int) -> bool:
    """Count a failed try. After `max_attempts` the pull is dropped (a later automatic run
    queues it again). Returns whether it was dropped."""
    attempts = item.attempts + 1
    if attempts >= max_attempts:
        conn.execute(
            "DELETE FROM capture_queue WHERE osu_id = ? AND beatmap_id = ? AND gamemode = ?",
            (item.osu_id, item.beatmap_id, item.gamemode),
        )
        return True
    conn.execute(
        "UPDATE capture_queue SET attempts = ? WHERE osu_id = ? AND beatmap_id = ? "
        "AND gamemode = ?",
        (attempts, item.osu_id, item.beatmap_id, item.gamemode),
    )
    return False


def queue_size(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM capture_queue").fetchone()["n"])


def _marks(values: Collection[int]) -> str:
    return ", ".join("?" for _ in values)
