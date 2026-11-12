import json

import pytest

from app.domain.parsing import MAX_DIGITS, parse_uint
from app.repos import deleted_tournaments as deleted
from app.services.links import map_id, match_id, source_link


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("7", 7),
        (" 42 ", 42),
        ("007", 7),
        ("9" * MAX_DIGITS, int("9" * MAX_DIGITS)),
    ],
)
def test_digits_become_a_number(text, expected):
    assert parse_uint(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  ",
        "-1",
        "+1",
        "1.5",
        "1e3",
        "abc",
        "12 3",
        "١٢٣",
        "²",
        "9" * (MAX_DIGITS + 1),
        "9" * 5000,
    ],
)
def test_anything_else_is_not_a_number(text):
    assert parse_uint(text) is None


def test_links_with_absurdly_long_numbers_give_no_id():
    huge = "9" * 5000
    assert map_id(huge) is None
    assert map_id(f"https://osu.ppy.sh/b/{huge}") is None
    assert match_id(f"https://osu.ppy.sh/community/matches/{huge}") is None
    assert map_id("9" * MAX_DIGITS) == int("9" * MAX_DIGITS)


# --- remembered deletions ---


def test_remembered_deletions_match_by_id_or_slug(db):
    assert not deleted.is_deleted(db, 5, "cup")

    deleted.remember(db, 5, "cup")

    assert deleted.is_deleted(db, 5, None)
    assert deleted.is_deleted(db, None, "cup")
    assert deleted.is_deleted(db, 5, "other")
    assert not deleted.is_deleted(db, 6, "other")
    assert not deleted.is_deleted(db, None, None)


def test_remembering_twice_stores_once_and_forgetting_removes_both(db):
    deleted.remember(db, 5, "cup")
    deleted.remember(db, 5, "cup")
    stored = json.loads(
        db.execute("SELECT value FROM app_settings WHERE key = ?", (deleted.KEY,)).fetchone()[0]
    )
    assert stored == {"ids": [5], "slugs": ["cup"]}

    deleted.forget(db, 5, "cup")

    assert not deleted.is_deleted(db, 5, "cup")


def test_a_damaged_value_remembers_nothing_and_does_not_crash(db):
    for broken in ("not json", "[1, 2]", '{"ids": "x"}', "null"):
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (deleted.KEY, broken),
        )
        assert not deleted.is_deleted(db, 5, "cup")
        deleted.remember(db, 5, "cup")  # and writing repairs it
        assert deleted.is_deleted(db, 5, "cup")


def test_the_list_cannot_grow_without_limit(db, monkeypatch):
    monkeypatch.setattr(deleted, "MAX_REMEMBERED", 3)

    for number in range(1, 6):
        deleted.remember(db, number, f"cup{number}")

    assert not deleted.is_deleted(db, 1, None)
    assert deleted.is_deleted(db, 5, None) and deleted.is_deleted(db, 3, None)


# --- score and match links typed next to a score ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://osu.ppy.sh/scores/4242", "https://osu.ppy.sh/scores/4242"),
        ("https://osu.ppy.sh/scores/osu/4242", "https://osu.ppy.sh/scores/4242"),
        ("osu.ppy.sh/scores/fruits/99?x=1#frag", "https://osu.ppy.sh/scores/99"),
        ("https://osu.ppy.sh/community/matches/777", "https://osu.ppy.sh/community/matches/777"),
        ("https://osu.ppy.sh/mp/777", None),
        ("  https://evil.example/scores/5?next=//x  ", "https://osu.ppy.sh/scores/5"),
        ("https://osu.ppy.sh/beatmaps/129891", None),
        ("12345", None),
        ("", None),
        ("https://osu.ppy.sh/scores/" + "9" * 40, None),
    ],
)
def test_source_links_are_rebuilt_from_their_id(text, expected):
    assert source_link(text) == expected
