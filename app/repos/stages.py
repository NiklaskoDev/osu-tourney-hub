"""SQL for stages and their pool maps."""

import sqlite3
from typing import Any

from app.domain.models import PoolMap, Stage

_SELECT_STAGE = (
    "SELECT id, tournament_id, wybin_stage_id, name, sort_order, start_at, end_at, best_of, bans, "
    "protects, is_qualifier, lineup_size, pool_released FROM stages "
)
_SELECT_MAP = (
    "SELECT id, stage_id, slot, bracket, required_mods, is_freemod, is_tiebreaker, sort_order, "
    "beatmap_id, beatmapset_id, artist, title, version, mapper, star_rating, bpm, length_s, "
    "cs, ar, od, hp FROM pool_maps "
)
_MAP_VALUE_COLUMNS = (
    "bracket",
    "required_mods",
    "is_freemod",
    "is_tiebreaker",
    "sort_order",
    "beatmap_id",
    "beatmapset_id",
    "artist",
    "title",
    "version",
    "mapper",
    "star_rating",
    "bpm",
    "length_s",
    "cs",
    "ar",
    "od",
    "hp",
    "wybin_entry_id",
)


def get(conn: sqlite3.Connection, stage_id: int) -> Stage | None:
    row = conn.execute(_SELECT_STAGE + "WHERE id = ?", (stage_id,)).fetchone()
    return _stage(row) if row else None


def list_for_tournament(conn: sqlite3.Connection, tournament_id: int) -> list[Stage]:
    rows = conn.execute(
        _SELECT_STAGE + "WHERE tournament_id = ? ORDER BY sort_order, id",
        (tournament_id,),
    ).fetchall()
    return [_stage(row) for row in rows]


def find_by_wybin_id(conn: sqlite3.Connection, wybin_stage_id: int) -> Stage | None:
    row = conn.execute(_SELECT_STAGE + "WHERE wybin_stage_id = ?", (wybin_stage_id,)).fetchone()
    return _stage(row) if row else None


def next_sort_order(conn: sqlite3.Connection, tournament_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next FROM stages WHERE tournament_id = ?",
        (tournament_id,),
    ).fetchone()
    return int(row["next"])


def insert(conn: sqlite3.Connection, tournament_id: int, values: dict[str, Any]) -> int:
    columns = ["tournament_id", *values]
    cursor = conn.execute(
        f"INSERT INTO stages ({', '.join(columns)}) "  # noqa: S608 - names are fixed by callers
        f"VALUES ({', '.join('?' for _ in columns)})",
        [tournament_id, *values.values()],
    )
    return int(cursor.lastrowid)


def update_from_wybin(conn: sqlite3.Connection, stage_id: int, values: dict[str, Any]) -> None:
    """Refresh what wybin owns. `lineup_size` is the app's own setting and is never touched."""
    assignments = ", ".join(f"{column} = ?" for column in values)
    conn.execute(
        f"UPDATE stages SET {assignments} WHERE id = ?",  # noqa: S608 - names fixed by callers
        [*values.values(), stage_id],
    )


def set_lineup_size(conn: sqlite3.Connection, stage_id: int, lineup_size: int | None) -> None:
    conn.execute("UPDATE stages SET lineup_size = ? WHERE id = ?", (lineup_size, stage_id))


def set_pool_released(conn: sqlite3.Connection, stage_id: int, released: bool) -> None:
    conn.execute("UPDATE stages SET pool_released = ? WHERE id = ?", (int(released), stage_id))


def delete(conn: sqlite3.Connection, stage_id: int) -> None:
    conn.execute("DELETE FROM stages WHERE id = ?", (stage_id,))


def list_maps(conn: sqlite3.Connection, stage_id: int) -> list[PoolMap]:
    rows = conn.execute(
        _SELECT_MAP + "WHERE stage_id = ? ORDER BY sort_order, id",
        (stage_id,),
    ).fetchall()
    return [_pool_map(row) for row in rows]


def get_map(conn: sqlite3.Connection, map_id: int) -> PoolMap | None:
    row = conn.execute(_SELECT_MAP + "WHERE id = ?", (map_id,)).fetchone()
    return _pool_map(row) if row else None


def count_maps(conn: sqlite3.Connection, stage_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pool_maps WHERE stage_id = ?", (stage_id,)
    ).fetchone()
    return int(row["n"])


def next_map_sort_order(conn: sqlite3.Connection, stage_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next FROM pool_maps WHERE stage_id = ?",
        (stage_id,),
    ).fetchone()
    return int(row["next"])


def insert_map(conn: sqlite3.Connection, stage_id: int, slot: str, values: dict[str, Any]) -> int:
    """Insert a pool map. Raises `sqlite3.IntegrityError` when the slot is taken in the stage."""
    columns = ["stage_id", "slot", *_MAP_VALUE_COLUMNS]
    cursor = conn.execute(
        f"INSERT INTO pool_maps ({', '.join(columns)}) "  # noqa: S608 - fixed column list
        f"VALUES ({', '.join('?' for _ in columns)})",
        [stage_id, slot, *(values.get(column) for column in _MAP_VALUE_COLUMNS)],
    )
    return int(cursor.lastrowid)


def replace_wybin_maps(
    conn: sqlite3.Connection, stage_id: int, maps: dict[str, dict[str, Any]]
) -> None:
    """Make the stage's maps exactly `maps` (slot -> values): update, insert, drop the rest.

    A stored row is matched to a new entry by its beatmap first and by its slot second, so a pool
    wybin reorders keeps each row (and whatever hangs off its id) on the same beatmap. Slots are
    parked on unique placeholders first, because two rows may swap slots in one update."""
    rows = conn.execute(
        "SELECT id, slot, beatmap_id FROM pool_maps WHERE stage_id = ?", (stage_id,)
    ).fetchall()
    free = {row["id"]: row for row in rows}
    matched: dict[str, int] = {}  # new slot -> stored row id
    for slot, values in maps.items():
        beatmap_id = values.get("beatmap_id")
        if beatmap_id is not None:
            row_id = _take_row(free, "beatmap_id", beatmap_id)
            if row_id is not None:
                matched[slot] = row_id
    for slot in maps:
        if slot not in matched:
            row_id = _take_row(free, "slot", slot)
            if row_id is not None:
                matched[slot] = row_id
    conn.execute("UPDATE pool_maps SET slot = '#' || id WHERE stage_id = ?", (stage_id,))
    for row_id in free:  # stored rows no new entry claimed
        conn.execute("DELETE FROM pool_maps WHERE id = ?", (row_id,))
    assignments = ", ".join(f"{column} = ?" for column in _MAP_VALUE_COLUMNS)
    for slot, values in maps.items():
        if slot in matched:
            conn.execute(
                f"UPDATE pool_maps SET slot = ?, {assignments} WHERE id = ?",  # noqa: S608
                [slot, *(values.get(column) for column in _MAP_VALUE_COLUMNS), matched[slot]],
            )
        else:
            insert_map(conn, stage_id, slot, values)


def _take_row(free: dict[int, sqlite3.Row], column: str, value: object) -> int | None:
    """Remove and return the id of the first unclaimed stored row whose `column` equals `value`."""
    for row_id, row in free.items():
        if row[column] == value:
            del free[row_id]
            return row_id
    return None


def delete_map(conn: sqlite3.Connection, map_id: int) -> None:
    conn.execute("DELETE FROM pool_maps WHERE id = ?", (map_id,))


def _stage(row: sqlite3.Row) -> Stage:
    values = dict(row)
    values["is_qualifier"] = bool(values["is_qualifier"])
    values["pool_released"] = bool(values["pool_released"])
    return Stage(**values)


def _pool_map(row: sqlite3.Row) -> PoolMap:
    values = dict(row)
    values["is_freemod"] = bool(values["is_freemod"])
    values["is_tiebreaker"] = bool(values["is_tiebreaker"])
    return PoolMap(**values)
