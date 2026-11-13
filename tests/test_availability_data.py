"""Availability as data: the CSV export, and a person's own export and deletion."""

import csv
import io

import pytest

from tests.support import ADMIN_ID, add_availability, add_member, add_tournament, osu_login

CAPTAIN, PLAYER, GUEST, STRANGER = 2001, 2002, 2004, 2009


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    add_member(db, tid, GUEST, "guest")
    return tid


def export(client, tid):
    return client.get(f"/t/{tid}/export/availability.csv")


def rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


# --- the CSV -----------------------------------------------------------------------------------


def test_the_export_lists_utc_slot_player_and_status_in_slot_order(client, login, db, cup):
    add_availability(db, cup, PLAYER, ["2026-11-16T18:30:00Z", "2026-11-16T18:00:00Z"], "yes")
    add_availability(db, cup, CAPTAIN, ["2026-11-16T18:00:00Z"], "maybe")
    login(PLAYER)

    response = export(client, cup)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert rows(response) == [
        ["slot_utc", "player", "osu_id", "status"],
        ["2026-11-16T18:00:00Z", "player2001", "2001", "maybe"],
        ["2026-11-16T18:00:00Z", "player2002", "2002", "yes"],
        ["2026-11-16T18:30:00Z", "player2002", "2002", "yes"],
    ]


def test_the_export_uses_crlf_and_a_named_attachment(client, login, cup):
    login(PLAYER)

    response = export(client, cup)

    assert response.text == "slot_utc,player,osu_id,status\r\n"  # header only when nothing is saved
    assert response.headers["content-disposition"] == 'attachment; filename="SC-availability.csv"'


def test_no_rows_are_listed_for_no_answers_or_for_guests_and_former_members(client, login, db, cup):
    add_availability(db, cup, PLAYER, ["2026-11-16T18:00:00Z"], "no")
    add_availability(db, cup, GUEST, ["2026-11-16T18:00:00Z"], "yes")
    add_availability(db, cup, 2077, ["2026-11-16T18:00:00Z"], "yes")  # left the roster
    login(PLAYER)

    assert rows(export(client, cup)) == [["slot_utc", "player", "osu_id", "status"]]


def test_the_export_only_has_this_tournament(client, login, db, cup):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_member(db, other, PLAYER, "player")
    add_availability(db, other, PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(PLAYER)

    assert len(rows(export(client, cup))) == 1


def test_a_formula_looking_name_is_neutralised(client, login, db, cup):
    add_availability(db, cup, PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    login(PLAYER)
    db.execute("UPDATE users SET username = '=HYPERLINK(1)' WHERE osu_id = ?", (PLAYER,))

    assert rows(export(client, cup))[1][1] == "'=HYPERLINK(1)"


def test_guests_may_export_and_strangers_may_not(client, login, cup):
    login(GUEST)
    assert export(client, cup).status_code == 200
    login(STRANGER)
    assert export(client, cup).status_code == 404


def test_an_admin_may_export(client, login, cup):
    login(ADMIN_ID)

    assert export(client, cup).status_code == 200


def test_an_archived_tournament_can_still_be_exported(client, login, db, cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (cup,))
    login(PLAYER)

    assert export(client, cup).status_code == 200


def test_the_availability_page_links_to_the_export(client, login, cup):
    login(PLAYER)

    assert f"/t/{cup}/export/availability.csv" in client.get(f"/t/{cup}/availability").text


# --- a person's own data -----------------------------------------------------------------------


def test_the_personal_export_includes_my_availability_and_nobody_elses(client, login, db, cup):
    add_availability(db, cup, PLAYER, ["2026-11-16T18:00:00Z", "2026-11-16T18:30:00Z"], "yes")
    add_availability(db, cup, CAPTAIN, ["2026-11-16T18:00:00Z"], "maybe")
    login(PLAYER)

    data = client.get("/me/export").json()

    assert data["availability"] == [
        {"tournament_id": cup, "slot_start": "2026-11-16T18:00:00Z", "status": "yes"},
        {"tournament_id": cup, "slot_start": "2026-11-16T18:30:00Z", "status": "yes"},
    ]


def test_deleting_my_data_deletes_my_availability_but_not_others(client, osu, csrf, db, cup):
    osu.add_user(PLAYER, "SampleUser")
    add_availability(db, cup, PLAYER, ["2026-11-16T18:00:00Z"], "yes")
    add_availability(db, cup, CAPTAIN, ["2026-11-16T18:00:00Z"], "yes")
    osu_login(client, PLAYER)

    response = client.post("/me/delete", data=csrf(), follow_redirects=False)

    assert response.status_code == 303
    left = db.execute("SELECT osu_id FROM availability").fetchall()
    assert [row["osu_id"] for row in left] == [CAPTAIN]
