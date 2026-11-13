"""SQL for availability slots. A slot without a row means "no"."""

import sqlite3
from collections.abc import Collection

AvailabilityMap = dict[int, dict[str, str]]  # osu_id -> slot start (UTC text) -> yes | maybe


def list_range(
    conn: sqlite3.Connection,
    tournament_id: int,
    osu_ids: Collection[int],
    start: str,
    end: str,
) -> AvailabilityMap:
    """The yes and maybe slots of the given people with `start <= slot < end`."""
    if not osu_ids:
        return {}
    marks = ", ".join("?" for _ in osu_ids)
    rows = conn.execute(
        "SELECT osu_id, slot_start, status FROM availability "  # noqa: S608
        f"WHERE tournament_id = ? AND osu_id IN ({marks}) AND slot_start >= ? AND slot_start < ? "
        "AND status != 'no'",
        [tournament_id, *osu_ids, start, end],
    ).fetchall()
    found: AvailabilityMap = {}
    for row in rows:
        found.setdefault(row["osu_id"], {})[row["slot_start"]] = row["status"]
    return found


def set_slots(
    conn: sqlite3.Connection, tournament_id: int, osu_id: int, slots: Collection[str], status: str
) -> None:
    """Set every slot to `status`. "no" removes the row, so only real answers are stored."""
    if status == "no":
        conn.executemany(
            "DELETE FROM availability WHERE tournament_id = ? AND osu_id = ? AND slot_start = ?",
            [(tournament_id, osu_id, slot) for slot in slots],
        )
        return
    conn.executemany(
        "INSERT INTO availability (tournament_id, osu_id, slot_start, status) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (tournament_id, osu_id, slot_start) DO UPDATE SET status = excluded.status",
        [(tournament_id, osu_id, slot, status) for slot in slots],
    )


def list_all(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    """Every stored slot of a tournament, with the person's name when known, in slot order."""
    return conn.execute(
        "SELECT a.slot_start, a.osu_id, u.username, a.status FROM availability a "
        "LEFT JOIN users u ON u.osu_id = a.osu_id WHERE a.tournament_id = ? "
        "ORDER BY a.slot_start, u.username COLLATE NOCASE, a.osu_id",
        (tournament_id,),
    ).fetchall()


def delete_for_member(conn: sqlite3.Connection, tournament_id: int, osu_id: int) -> None:
    conn.execute(
        "DELETE FROM availability WHERE tournament_id = ? AND osu_id = ?", (tournament_id, osu_id)
    )
