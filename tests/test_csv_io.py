"""CSV export of the sheet and lineup, and the score import with its preview step."""

import html
import re

import pytest

from app.repos import stages as stages_repo
from app.repos import tournaments as tournaments_repo
from app.services import csv_io
from app.services.csv_text import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    CsvImportError,
    clean_cell,
    guard_cell,
    parse_table,
)
from app.web.routes.sheet_io import SHOWN_UNKNOWN
from tests.support import (
    ADMIN_ID,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER_PLAYER, GUEST, STRANGER = 2001, 2002, 2003, 2004, 2009
HEADER = "slot,player,score,accuracy,misses,mods"


@pytest.fixture
def cup(db):
    """Spring Cup (osu!, ScoreV2): a captain, two players (player2002, player2003) and a guest;
    stage Qualifiers with NM1 (1001), HD1 (1002, needs HD), FM1 (1003) and TB (1004); stage
    Finals with NM1 (1005)."""
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, OTHER_PLAYER, "player")
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid)
    add_pool_map(db, stage, "NM1")
    add_pool_map(db, stage, "HD1", required_mods="HD")
    add_pool_map(db, stage, "FM1", freemod=True)
    add_pool_map(db, stage, "TB", tiebreaker=True)
    finals = add_stage(db, tid, "Finals", 2)
    add_pool_map(db, finals, "NM1", beatmap_id=1005)
    return {"id": tid, "stage": stage, "finals": finals}


def imported(db):
    rows = db.execute(
        "SELECT beatmap_id, osu_id, mods, score, scoring, accuracy, misses, source, entered_by, "
        "tournament_id, achieved_at, max_combo, source_url FROM scores WHERE source = 'import' "
        "ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


# --- text handling ---


def test_a_table_is_read_with_its_header_rows_and_line_numbers():
    table = parse_table("a,b\n1,2\n\n3,4\n")

    assert table.headers == ["a", "b"]
    assert table.rows == [["1", "2"], ["3", "4"]]
    assert table.lines == [2, 4]  # the empty line still counts


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_the_delimiter_is_detected_from_the_header(delimiter):
    table = parse_table(delimiter.join(["slot", "score"]) + "\n" + delimiter.join(["NM1", "5"]))

    assert table.headers == ["slot", "score"] and table.rows == [["NM1", "5"]]


def test_a_quoted_cell_may_contain_the_delimiter_and_a_line_break():
    table = parse_table('slot,player,score\nNM1,"Smith, Jo\nAnn",5\nHD1,x,6')

    assert table.rows[0] == ["NM1", "Smith, Jo\nAnn", "5"]
    assert table.lines == [2, 4]  # the next row starts on line 4


@pytest.mark.parametrize("text", ["", "   \n\n", "slot,score\n", "slot,score\n,\n  ,  \n"])
def test_a_file_without_data_rows_is_refused(text):
    with pytest.raises(CsvImportError, match="no rows"):
        parse_table(text)


def test_the_size_and_row_limits_are_exact():
    row = "NM1,player2002,1,90\n"
    header = "slot,player,score,accuracy\n"
    parse_table(header + row * MAX_CSV_ROWS)  # exactly the row limit
    with pytest.raises(CsvImportError, match="more than 5000 rows"):
        parse_table(header + row * (MAX_CSV_ROWS + 1))
    exactly = header + row + "\n" * (MAX_CSV_BYTES - len(header + row))
    assert len(exactly.encode()) == MAX_CSV_BYTES
    parse_table(exactly)
    with pytest.raises(CsvImportError, match="larger than 1 MB"):
        parse_table(exactly + "\n")


def test_a_nul_byte_makes_the_file_unreadable():
    with pytest.raises(CsvImportError, match="not a readable CSV"):
        parse_table("a,b\n\x00,1")


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@SUM", "\tx", "\rx"])
def test_formula_looking_cells_are_guarded_on_export_and_restored_on_import(text):
    guarded = guard_cell(text)

    assert guarded == "'" + text
    assert clean_cell(guarded) == text


@pytest.mark.parametrize("text", ["player", "O'Brien", "'quoted'", "1,5", ""])
def test_ordinary_cells_are_left_alone(text):
    assert guard_cell(text) == text
    assert clean_cell(f"  {text}  ") == text


# --- export ---


def test_the_pool_export_has_one_row_per_players_best_score(client, login, db, cup):
    add_score(db, 1001, PLAYER, 800_000, accuracy=96.0)
    best = add_score(db, 1001, PLAYER, 900_000, accuracy=98.0, source="api", scoring="v2")
    db.execute("UPDATE scores SET misses = 2 WHERE id = ?", (best,))
    add_score(db, 1003, OTHER_PLAYER, 700_000, accuracy=97.123, mods="HDHR")
    add_score(db, 1001, CAPTAIN, 990_000, scoring="v1")  # another scoring type: does not count
    add_score(db, 1001, GUEST, 500_000)  # guests are not on the sheet
    login(PLAYER)

    response = client.get(f"/t/{cup['id']}/export/pool.csv")

    assert response.status_code == 200
    assert response.text == (
        "slot,beatmap_id,player,osu_id,score,accuracy,misses,mods,source\r\n"
        "NM1,1001,player2002,2002,900000,98.00,2,NM,api\r\n"
        "FM1,1003,player2003,2003,700000,97.12,,HDHR,manual\r\n"
    )


def test_the_export_is_a_download_named_after_the_tournament_and_stage(client, login, db, cup):
    login(GUEST)  # everyone on the roster may export

    response = client.get(f"/t/{cup['id']}/export/pool.csv")

    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="SC-Qualifiers-pool.csv"'
    )


def test_the_export_follows_the_stage_in_the_address(client, login, db, cup):
    add_score(db, 1005, PLAYER, 600_000)
    login(PLAYER)

    finals = client.get(f"/t/{cup['id']}/export/pool.csv?stage={cup['finals']}")

    assert finals.text == (
        "slot,beatmap_id,player,osu_id,score,accuracy,misses,mods,source\r\n"
        "NM1,1005,player2002,2002,600000,97.00,,NM,manual\r\n"
    )
    assert 'filename="SC-Finals-pool.csv"' in finals.headers["content-disposition"]
    assert client.get(f"/t/{cup['id']}/export/pool.csv").text.count("\r\n") == 1  # first stage


def test_only_the_roster_can_export(client, login, cup):
    login(STRANGER)

    assert client.get(f"/t/{cup['id']}/export/pool.csv").status_code == 404
    assert client.get(f"/t/{cup['id']}/export/lineup.csv").status_code == 404


def test_a_tournament_without_stages_has_nothing_to_export(client, login, db):
    tid = add_tournament(db, "Bare Cup", acronym="BC")
    add_member(db, tid, PLAYER, "player")
    login(PLAYER)

    assert client.get(f"/t/{tid}/export/pool.csv").status_code == 404


@pytest.mark.parametrize("name", ["=SUM(A1)", "+1", "-Kai-", "@home"])
def test_a_player_name_cannot_run_as_a_formula_in_a_spreadsheet(client, login, db, cup, name):
    login(PLAYER)  # logging in refreshes the stored name, so rename afterwards
    db.execute("UPDATE users SET username = ? WHERE osu_id = ?", (name, PLAYER))
    add_score(db, 1001, PLAYER, 900_000)

    text = client.get(f"/t/{cup['id']}/export/pool.csv").text

    assert f",'{name},{PLAYER}," in text


def test_the_lineup_export_lists_the_saved_lineup(client, login, db, cup):
    lineup = db.execute(
        "INSERT INTO lineups (tournament_id, stage_id, status, updated_at) "
        "VALUES (?, ?, 'final', '2026-11-12T12:00:00Z')",
        (cup["id"], cup["stage"]),
    ).lastrowid
    first = db.execute(
        "SELECT id FROM pool_maps WHERE slot = 'NM1' AND beatmap_id = 1001"
    ).fetchone()
    third = db.execute("SELECT id FROM pool_maps WHERE slot = 'FM1'").fetchone()
    for map_id, osu_id, mods in (
        (third[0], PLAYER, "HR"),
        (first[0], OTHER_PLAYER, ""),
        (first[0], 5555, ""),
    ):
        db.execute(
            "INSERT INTO lineup_entries (lineup_id, pool_map_id, osu_id, mods) VALUES (?, ?, ?, ?)",
            (lineup, map_id, osu_id, mods),
        )
    login(PLAYER)

    response = client.get(f"/t/{cup['id']}/export/lineup.csv")

    assert response.text == (
        "slot,beatmap_id,player,osu_id,mods,status\r\n"
        "NM1,1001,player2003,2003,NM,final\r\n"
        "NM1,1001,osu! user 5555,5555,NM,final\r\n"
        "FM1,1003,player2002,2002,HR,final\r\n"
    )
    assert 'filename="SC-Qualifiers-lineup.csv"' in response.headers["content-disposition"]


def test_without_a_saved_lineup_the_export_is_just_the_header(client, login, cup):
    login(PLAYER)

    assert client.get(f"/t/{cup['id']}/export/lineup.csv").text == (
        "slot,beatmap_id,player,osu_id,mods,status\r\n"
    )


def test_the_export_links_exist_on_the_lineup_page_once_a_lineup_is_saved(client, login, db, cup):
    login(CAPTAIN)
    assert "export/lineup.csv" not in client.get(f"/t/{cup['id']}/lineup").text
    db.execute(
        "INSERT INTO lineups (tournament_id, stage_id, status, updated_at) "
        "VALUES (?, ?, 'draft', '2026-11-12T12:00:00Z')",
        (cup["id"], cup["stage"]),
    )

    assert f"export/lineup.csv?stage={cup['stage']}" in client.get(f"/t/{cup['id']}/lineup").text


# --- column detection ---


def test_columns_are_found_by_common_header_names():
    mapping = csv_io.detect_mapping(
        ["Map ID", "Username", "Total_Score", "Acc %", "Miss", "Mod", "Notes"]
    )

    assert mapping == {
        "slot": None,
        "beatmap": 0,
        "player": 1,
        "osu_id": None,
        "score": 2,
        "accuracy": 3,
        "misses": 4,
        "mods": 5,
    }


def test_the_exports_own_header_maps_completely():
    mapping = csv_io.detect_mapping(list(csv_io.POOL_HEADER))

    assert csv_io.mapping_problems(mapping) == []
    assert mapping["slot"] == 0 and mapping["osu_id"] == 3


def test_missing_columns_are_named():
    problems = csv_io.mapping_problems(csv_io.detect_mapping(["a", "b"]))

    assert problems == [
        "Choose the column with the map slot or the beatmap id.",
        "Choose the column with the player name or osu! id.",
        "Choose the column with the score.",
    ]


def test_a_chosen_column_that_does_not_exist_counts_as_not_in_the_file():
    mapping = csv_io.mapping_from_form({"map_slot": "5", "map_score": "x", "map_mods": "1"}, 2)

    assert mapping["slot"] is None and mapping["score"] is None and mapping["mods"] == 1


# --- importing: the steps a captain takes ---


def upload(client, token, tid, text, name="scores.csv", **extra):
    data = text if isinstance(text, bytes) else text.encode()
    return client.post(
        f"/t/{tid}/import",
        data={**token, **extra},
        files={"file": (name, data, "text/csv")},
        follow_redirects=False,
    )


def form_of_commit(page: str) -> dict[str, str]:
    """What the browser would post from the preview page's import button."""
    assert "/import/commit" in page, "no commit form on the page"
    form = page.split("/import/commit", 1)[1].split("</form>", 1)[0]
    values = {name: value for name, value in re.findall(r'name="([\w]+)" value="([^"]*)"', form)}
    csv_text = re.search(r'<textarea name="csv_text" hidden>(.*?)</textarea>', form, re.S)
    assert csv_text, "no commit form on the page"
    return {**values, "csv_text": html.unescape(csv_text.group(1))}


def import_all(client, token, tid, text, **extra):
    preview = upload(client, token, tid, text, **extra)
    assert preview.status_code == 200, preview.text
    return client.post(
        f"/t/{tid}/import/commit",
        data={**token, **form_of_commit(preview.text)},
        follow_redirects=False,
    )


GOOD = (
    f"{HEADER}\n"
    "NM1,player2002,900000,98.5,2,\n"
    "HD1,player2003,800000,97.25,,\n"
    'FM1,player2002,700000,96,1,"HD, HR"\n'
)


def test_a_captain_uploads_a_file_checks_the_preview_and_imports(client, login, csrf, db, cup):
    login(CAPTAIN)
    token = csrf()

    preview = upload(client, token, cup["id"], GOOD)

    assert preview.status_code == 200
    assert "3 rows read" in preview.text and "3 new" in preview.text
    assert "player2002" in preview.text and "900,000" in preview.text and "98.50%" in preview.text
    assert "Import 3 scores" in preview.text
    assert imported(db) == []  # nothing is stored by the preview

    done = import_all(client, token, cup["id"], GOOD)

    assert done.status_code == 303
    assert done.headers["location"] == f"/t/{cup['id']}/pool?stage={cup['stage']}"
    assert imported(db) == [
        (1001, PLAYER, "", 900000, "v2", 98.5, 2, "import", CAPTAIN, cup["id"], None, None, None),
        (
            1002,
            OTHER_PLAYER,
            "HD",
            800000,
            "v2",
            97.25,
            None,
            "import",
            CAPTAIN,
            cup["id"],
            None,
            None,
            None,
        ),
        (
            1003,
            PLAYER,
            "HDHR",
            700000,
            "v2",
            96.0,
            1,
            "import",
            CAPTAIN,
            cup["id"],
            None,
            None,
            None,
        ),
    ]
    page = client.get(done.headers["location"]).text
    assert "Imported 3 scores." in page and "900,000" in page


def test_imported_scores_are_saved_in_the_tournaments_scoring_type(client, login, csrf, db, cup):
    db.execute("UPDATE tournaments SET scoring = 'lazer' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], GOOD)

    assert {row[4] for row in imported(db)} == {"lazer"}


def test_rows_may_name_players_by_osu_id_and_maps_by_beatmap_id(client, login, csrf, db, cup):
    text = "Map ID,Player ID,Score,Acc\n1001,2002,900000,98\n1002,2003,800000,97\n"
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], text)

    assert [(row[0], row[1], row[3]) for row in imported(db)] == [
        (1001, PLAYER, 900000),
        (1002, OTHER_PLAYER, 800000),
    ]


def test_names_match_without_regard_to_case_and_numbers_are_read_like_a_person_writes_them(
    client, login, csrf, db, cup
):
    text = "slot;player;score;accuracy;misses\nnm1;PLAYER2002;900 000;97,5%;3\n"
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], text)

    assert [(row[0], row[1], row[3], row[5], row[6]) for row in imported(db)] == [
        (1001, PLAYER, 900000, 97.5, 3)
    ]


def test_a_byte_order_mark_and_windows_line_ends_are_fine(client, login, csrf, db, cup):
    raw = b"\xef\xbb\xbf" + GOOD.replace("\n", "\r\n").encode()
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], raw)

    assert len(imported(db)) == 3


def test_slots_are_read_in_the_chosen_stage(client, login, csrf, db, cup):
    text = "slot,player,score,accuracy\nNM1,player2002,600000,95\n"
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], text, stage=str(cup["finals"]))

    assert [row[0] for row in imported(db)] == [1005]  # NM1 of the Finals, not of Qualifiers


def test_mods_follow_the_maps_rules(client, login, csrf, db, cup):
    rows = [
        "NM1,player2002,1,90,,NM",  # nomod map: NM is fine and stays no mods
        "NM1,player2002,2,90,,HD",  # a mod the map does not have
        "HD1,player2002,3,90,,",  # left out: the map's own mods
        "HD1,player2002,4,90,,HD",  # repeated: fine
        "HD1,player2002,5,90,,NM",  # contradicts the map
        "FM1,player2002,6,90,,HD+HR",
        "FM1,player2002,7,90,,",
        "FM1,player2002,8,90,,XX",  # not a mod
        "TB,player2002,9,90,,NF HD",  # NF is ignored; the rest counts
    ]
    login(CAPTAIN)
    token = csrf()

    preview = upload(client, token, cup["id"], HEADER + "\n" + "\n".join(rows))
    import_all(client, token, cup["id"], HEADER + "\n" + "\n".join(rows))

    assert [(row[0], row[2], row[3]) for row in imported(db)] == [
        (1001, "", 1),
        (1002, "HD", 3),
        (1002, "HD", 4),
        (1003, "HDHR", 6),
        (1003, "", 7),
        (1004, "HD", 9),
    ]
    assert "Mods HD do not match NM1." in preview.text
    assert "Mods NM do not match HD1." in preview.text
    assert "XX is not a mod in osu!." in preview.text


def test_bad_rows_are_listed_and_do_not_stop_the_good_ones(client, login, csrf, db, cup):
    rows = [
        "NM1,player2002,900000,98,,",  # good
        "NM9,player2002,900000,98,,",  # no such slot
        "NM1,player2002,lots,98,,",  # score is not a number
        "NM1,player2002,900000,101,,",  # accuracy over 100
        "NM1,player2002,900000,98,-1,",  # misses not a count
        "NM1,,900000,98,,",  # no player
        ",player2002,900000,98,,",  # no map
        "NM1,ghost,900000,98,,",  # not on the roster
        "NM1,player2004,900000,98,,",  # a guest: not on the sheet
        "NM1,ghost,800000,98,,",  # the same unknown player again
    ]
    login(CAPTAIN)
    token = csrf()
    text = HEADER + "\n" + "\n".join(rows)

    preview = upload(client, token, cup["id"], text).text

    assert (
        "10 rows read" in preview and "1 new" in preview and "9 that cannot be imported" in preview
    )
    assert "There is no map NM9 in Qualifiers." in preview
    assert "Enter the score as a whole number" in preview
    assert "Enter the accuracy as a number from 0 to 100" in preview
    assert "Misses must be a whole number" in preview
    assert "The row names no player." in preview and "The row names no map." in preview
    done = import_all(client, token, cup["id"], text)
    assert len(imported(db)) == 1
    assert "9 rows were left out" in client.get(done.headers["location"]).text


def csv_of(rows):
    return HEADER + "\n" + "\n".join(rows)


def plan_for(db, cup, rows):
    tournament = tournaments_repo.get(db, cup["id"])
    stage = stages_repo.get(db, cup["stage"])
    table = parse_table(csv_of(rows))
    return csv_io.plan_import(db, tournament, stage, table, csv_io.detect_mapping(table.headers))


def test_players_not_on_the_roster_are_named_once_each_in_the_preview(client, login, csrf, db, cup):
    rows = [
        "NM1,ghost,900000,98,,",
        "NM1,player2002,900000,98,,",  # good
        "NM1,player2004,900000,98,,",  # a guest: not on the sheet
        "NM1,ghost,800000,98,,",  # the same unknown player again
    ]

    plan = plan_for(db, cup, rows)

    assert plan.unknown_players == ["ghost", "player2004"]
    login(CAPTAIN)
    preview = upload(client, csrf(), cup["id"], csv_of(rows)).text
    assert "Not on the roster" in preview
    assert "<p>ghost, player2004. Add them on the Members page" in preview


def test_a_long_list_of_players_not_on_the_roster_is_cut_short_in_the_preview(
    client, login, csrf, db, cup
):
    names = [f"ghost{number:03d}" for number in range(SHOWN_UNKNOWN + 2)]
    rows = [f"NM1,{name},900000,98,," for name in names]
    login(CAPTAIN)

    preview = upload(client, csrf(), cup["id"], csv_of(rows)).text

    assert plan_for(db, cup, rows).unknown_players == names
    assert f"<p>{', '.join(names[:SHOWN_UNKNOWN])} and 2 more. Add them on the Members page" in (
        preview
    )
    assert names[SHOWN_UNKNOWN] not in preview.split("Rows that cannot be imported")[0]


def test_the_same_score_twice_in_a_file_is_imported_once(client, login, csrf, db, cup):
    text = HEADER + "\n" + "NM1,player2002,900000,98,,\n" * 2 + "NM1,player2002,900001,98,,\n"
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], text)

    assert [row[3] for row in imported(db)] == [900000, 900001]


def test_a_score_already_on_the_sheet_is_skipped_whatever_its_source(client, login, csrf, db, cup):
    add_score(db, 1001, PLAYER, 900_000, source="manual")
    add_score(db, 1003, PLAYER, 700_000, mods="HD", source="api")
    text = (
        f"{HEADER}\nNM1,player2002,900000,98,,\n"  # same player, map, mods and score
        "FM1,player2002,700000,96,,HD\n"  # the same, already captured from osu!
        "FM1,player2002,700000,96,,HR\n"  # other mods: a different score
        "NM1,player2003,900000,98,,\n"  # other player
    )
    login(CAPTAIN)
    token = csrf()

    preview = upload(client, token, cup["id"], text).text
    import_all(client, token, cup["id"], text)

    assert "2 new" in preview and "2 already on the sheet" in preview
    assert [(row[1], row[2]) for row in imported(db)] == [(PLAYER, "HR"), (OTHER_PLAYER, "")]


def test_importing_the_same_file_again_adds_nothing(client, login, csrf, db, cup):
    login(CAPTAIN)
    token = csrf()
    import_all(client, token, cup["id"], GOOD)

    again = upload(client, token, cup["id"], GOOD)
    refused = client.post(
        f"/t/{cup['id']}/import/commit", data={**token, **form_of_commit(again.text)}
    )

    assert "0 new" in again.text and "3 already on the sheet" in again.text
    assert "Import 0 scores" in again.text
    assert refused.status_code == 422 and "No row can be imported" in refused.text
    assert len(imported(db)) == 3


def test_a_file_the_export_wrote_imports_back_even_with_formula_looking_names(
    client, login, csrf, db, cup
):
    db.execute("UPDATE users SET username = '-Kai-' WHERE osu_id = ?", (PLAYER,))
    add_score(db, 1001, PLAYER, 900_000, accuracy=98.0)
    add_score(db, 1003, OTHER_PLAYER, 700_000, accuracy=96.5, mods="HDHR")
    login(CAPTAIN)
    token = csrf()
    exported = client.get(f"/t/{cup['id']}/export/pool.csv").text
    db.execute("DELETE FROM scores")

    import_all(client, token, cup["id"], exported)

    assert [(row[0], row[1], row[2], row[3]) for row in imported(db)] == [
        (1001, PLAYER, "", 900000),
        (1003, OTHER_PLAYER, "HDHR", 700000),
    ]


# --- importing: columns the person chooses ---


def test_columns_that_cannot_be_recognised_are_chosen_on_the_preview(client, login, csrf, db, cup):
    text = "A,B,C,D\nNM1,player2002,900000,98\n"
    login(CAPTAIN)
    token = csrf()

    first = upload(client, token, cup["id"], text)

    assert first.status_code == 200
    assert "Choose the column with the map slot or the beatmap id." in first.text
    assert "Choose the column with the score." in first.text
    assert "What will be imported" not in first.text  # no rows are read until the columns are set
    chosen = {
        "csv_text": text,
        "stage": str(cup["stage"]),
        "map_slot": "0",
        "map_player": "1",
        "map_score": "2",
        "map_accuracy": "3",
    }
    second = client.post(f"/t/{cup['id']}/import/preview", data={**token, **chosen})
    assert "1 new" in second.text
    done = client.post(
        f"/t/{cup['id']}/import/commit", data={**token, **chosen}, follow_redirects=False
    )
    assert done.status_code == 303 and len(imported(db)) == 1


def test_committing_with_columns_missing_imports_nothing(client, login, csrf, db, cup):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/import/commit",
        data={**csrf(), "csv_text": "A,B\n1,2\n", "stage": str(cup["stage"])},
    )

    assert response.status_code == 422
    assert "No row can be imported" in response.text and imported(db) == []


def test_the_commit_checks_every_row_again_whatever_the_preview_said(client, login, csrf, db, cup):
    login(CAPTAIN)
    forged = {
        "csv_text": (
            f"{HEADER}\nNM1,player2004,900000,98,,\nNM1,ghost,1,1,,\nNM1,player2002,1,200,,\n"
        ),
        "stage": str(cup["stage"]),
        **{
            f"map_{name}": str(i)
            for i, name in enumerate(("slot", "player", "score", "accuracy", "misses", "mods"))
        },
    }

    response = client.post(f"/t/{cup['id']}/import/commit", data={**csrf(), **forged})

    assert response.status_code == 422 and imported(db) == []  # guest, unknown, accuracy 200


def test_a_stage_of_another_tournament_is_refused_on_commit(client, login, csrf, db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    foreign = add_stage(db, other)
    add_pool_map(db, foreign, "NM1", beatmap_id=1001)
    login(CAPTAIN)
    fields = {"csv_text": GOOD, "stage": str(foreign), "map_slot": "0", "map_player": "1",
              "map_score": "2", "map_accuracy": "3"}  # fmt: skip

    response = client.post(f"/t/{cup['id']}/import/commit", data={**csrf(), **fields})

    assert response.status_code == 422 and imported(db) == []


# --- importing: what is refused up front ---


def test_a_file_over_one_megabyte_is_refused_without_reading_it(client, login, csrf, db, cup):
    login(CAPTAIN)
    rows = "NM1,player2002,1,90\n"
    header = "slot,player,score,accuracy\n"
    exactly = header + rows + "\n" * (MAX_CSV_BYTES - len(header + rows))
    assert len(exactly.encode()) == MAX_CSV_BYTES

    accepted = upload(client, csrf(), cup["id"], exactly)
    refused = upload(client, csrf(), cup["id"], exactly + "\n")

    assert accepted.status_code == 200 and "1 new" in accepted.text
    assert refused.status_code == 422 and "larger than 1 MB" in refused.text
    assert imported(db) == []


def test_an_upload_far_over_the_limit_is_refused_before_the_form_is_parsed(
    client, login, csrf, db, cup
):
    login(CAPTAIN)

    response = upload(client, csrf(), cup["id"], b"a" * (2 * MAX_CSV_BYTES + 1))

    assert response.status_code == 413
    assert "larger than 2 MB" in response.text
    assert imported(db) == []


def test_a_file_with_more_than_5000_rows_is_refused(client, login, csrf, cup):
    login(CAPTAIN)
    rows = "NM1,player2002,1,90\n" * (MAX_CSV_ROWS + 1)

    response = upload(client, csrf(), cup["id"], "slot,player,score,accuracy\n" + rows)

    assert response.status_code == 422 and "more than 5000 rows" in response.text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"slot,player\n\xe9,\xff\n", "not UTF-8"),
        (b"", "no rows"),
        (b"slot,player,score,accuracy\n", "no rows"),
        (b"a,b\n\x00,1\n", "not a readable CSV"),
    ],
)
def test_unreadable_files_are_refused_with_a_reason(client, login, csrf, db, cup, content, message):
    login(CAPTAIN)

    response = upload(client, csrf(), cup["id"], content)

    assert response.status_code == 422 and message in response.text
    assert imported(db) == []


def test_posting_nothing_asks_for_a_file_or_a_link(client, login, csrf, cup):
    login(CAPTAIN)

    response = client.post(f"/t/{cup['id']}/import", data=csrf())

    assert response.status_code == 422 and "Choose a CSV file or paste the link" in response.text


def test_an_empty_file_field_counts_as_nothing_chosen(client, login, csrf, cup):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/import", data=csrf(), files={"file": ("", b"", "application/octet-stream")}
    )

    assert response.status_code == 422 and "Choose a CSV file" in response.text


def test_a_tournament_without_stages_cannot_receive_an_import(client, login, csrf, db):
    tid = add_tournament(db, "Bare Cup", acronym="BC")
    add_member(db, tid, CAPTAIN, "captain")
    login(CAPTAIN)

    assert "no stages" in client.get(f"/t/{tid}/import").text
    assert upload(client, csrf(), tid, GOOD).status_code == 422


def test_starting_an_import_is_limited_to_ten_an_hour_per_person(client, login, csrf, cup):
    login(CAPTAIN)
    token = csrf()
    statuses = [upload(client, token, cup["id"], b"", name="e.csv").status_code for _ in range(11)]

    assert statuses == [422] * 10 + [429]
    login(ADMIN_ID)
    assert upload(client, csrf(), cup["id"], GOOD).status_code == 200  # another person is fine


def test_text_from_the_file_is_shown_escaped(client, login, csrf, cup):
    text = (
        '<img src=x onerror=alert(1)>,player,score,accuracy\nNM1,"<script>alert(2)</script>",5,90\n'
    )
    login(CAPTAIN)

    page = upload(client, csrf(), cup["id"], text).text

    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page


# --- importing: who may ---


@pytest.mark.parametrize(("who", "status"), [(PLAYER, 403), (GUEST, 403), (STRANGER, 404)])
def test_only_captains_import(client, login, csrf, db, cup, who, status):
    login(who)
    token = csrf()
    fields = {"csv_text": GOOD, "stage": str(cup["stage"])}

    assert client.get(f"/t/{cup['id']}/import").status_code == status
    assert upload(client, token, cup["id"], GOOD).status_code == status
    assert (
        client.post(f"/t/{cup['id']}/import/preview", data={**token, **fields}).status_code
        == status
    )
    assert (
        client.post(f"/t/{cup['id']}/import/commit", data={**token, **fields}).status_code == status
    )
    assert imported(db) == []


def test_an_administrator_outside_the_roster_may_import(client, login, csrf, db, cup):
    login(ADMIN_ID)

    import_all(client, csrf(), cup["id"], GOOD)

    assert len(imported(db)) == 3 and imported(db)[0][8] == ADMIN_ID


def test_an_archived_tournament_takes_no_imports(client, login, csrf, db, cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup["id"],))
    login(CAPTAIN)
    token = csrf()
    fields = {"csv_text": GOOD, "stage": str(cup["stage"])}

    assert upload(client, token, cup["id"], GOOD).status_code == 409
    assert (
        client.post(f"/t/{cup['id']}/import/preview", data={**token, **fields}).status_code == 409
    )
    assert client.post(f"/t/{cup['id']}/import/commit", data={**token, **fields}).status_code == 409
    page = client.get(f"/t/{cup['id']}/import").text
    assert "This tournament is archived" in page and "Check file" not in page
    assert imported(db) == []


def test_every_import_step_needs_the_csrf_token(client, login, cup):
    login(CAPTAIN)
    fields = {"csv_text": GOOD, "stage": str(cup["stage"])}

    assert client.post(f"/t/{cup['id']}/import", data={}).status_code == 403
    assert client.post(f"/t/{cup['id']}/import/preview", data=fields).status_code == 403
    assert client.post(f"/t/{cup['id']}/import/commit", data=fields).status_code == 403


def test_the_import_page_offers_both_sources(client, login, cup):
    login(CAPTAIN)

    page = client.get(f"/t/{cup['id']}/import?stage={cup['finals']}").text

    assert 'enctype="multipart/form-data"' in page and 'type="file"' in page
    assert "Google Sheets link" in page and "docs.google.com" in page
    assert f'name="stage" value="{cup["finals"]}"' in page


def test_the_export_leaves_the_accuracy_empty_for_a_score_without_one(client, login, db, cup):
    add_score(db, 1001, PLAYER, 800_000, accuracy=None)
    login(PLAYER)

    text = client.get(f"/t/{cup['id']}/export/pool.csv").text

    assert "NM1,1001,player2002,2002,800000,,," in text


def test_a_file_the_export_wrote_with_a_blank_accuracy_can_be_imported_again(
    client, login, csrf, db, cup
):
    add_score(db, 1001, PLAYER, 800_000, accuracy=None)
    add_score(db, 1002, OTHER_PLAYER, 700_000, accuracy=96.5, mods="HD")
    login(CAPTAIN)
    exported = client.get(f"/t/{cup['id']}/export/pool.csv").text
    db.execute("DELETE FROM scores")
    assert "NM1,1001,player2002,2002,800000,,," in exported

    import_all(client, csrf(), cup["id"], exported)

    assert [(row[1], row[3], row[5]) for row in imported(db)] == [
        (PLAYER, 800_000, None),
        (OTHER_PLAYER, 700_000, 96.5),
    ]


def test_a_file_without_an_accuracy_column_imports_with_empty_accuracies(
    client, login, csrf, db, cup
):
    login(CAPTAIN)

    import_all(client, csrf(), cup["id"], "slot,player,score\nNM1,player2002,600000\n")

    assert [(row[3], row[5]) for row in imported(db)] == [(600_000, None)]


def test_an_accuracy_that_is_written_is_still_checked(client, login, csrf, db, cup):
    login(CAPTAIN)

    page = upload(client, csrf(), cup["id"], "slot,player,score,accuracy\nNM1,player2002,1,abc\n")

    assert "Enter the accuracy as a number from 0 to 100" in page.text
    assert imported(db) == []
