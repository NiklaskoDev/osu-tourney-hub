import pytest

from tests.support import ADMIN_ID, add_tournament


@pytest.fixture
def as_admin(client, login, csrf, db):
    login(ADMIN_ID, "AdminAmy")
    return csrf()


@pytest.fixture
def tid(db) -> int:
    return add_tournament(db, "Hand Cup", acronym="HC")  # gamemode 0, source manual


def post(client, path, data=None, token=None):
    return client.post(path, data={**(token or {}), **(data or {})}, follow_redirects=False)


def add_stage(client, token, tid, **overrides):
    data = {"name": "Round of 16", **overrides}
    return post(client, f"/admin/tournaments/{tid}/stages", data, token)


def add_map(client, token, tid, stage_id, slot="NM1", beatmap="129891"):
    data = {"stage_id": str(stage_id), "slot": slot, "beatmap": beatmap}
    return post(client, f"/admin/tournaments/{tid}/maps", data, token)


def stage_row(db, tid, name="Round of 16"):
    return db.execute(
        "SELECT * FROM stages WHERE tournament_id = ? AND name = ?", (tid, name)
    ).fetchone()


# --- the page ---


def test_the_page_shows_stages_and_maps_with_forms_for_a_manual_tournament(
    client, as_admin, tid, osu
):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid)
    add_map(client, as_admin, tid, 1)

    page = client.get(f"/admin/tournaments/{tid}").text

    assert "Round of 16" in page and "NM1" in page and "Synth Artist - Song [Insane]" in page
    assert "Add a stage" in page and "Add a map" in page
    assert "https://osu.ppy.sh/b/129891" in page


def test_a_new_manual_tournament_shows_an_empty_state_and_only_the_stage_form(
    client, as_admin, tid
):
    page = client.get(f"/admin/tournaments/{tid}").text

    assert "This tournament has no stages yet. Add the first one below." in page
    assert "Add a stage" in page
    assert "Add a map" not in page  # no stage to put it in


def test_a_wybin_tournament_is_shown_read_only(client, as_admin, db, wybin):
    wybin.load_defaults()
    client.post("/admin/tournaments/import", data={**as_admin, "link": "synthetic-cup"})
    imported = db.execute("SELECT id FROM tournaments WHERE wybin_id = 9001").fetchone()[0]

    page = client.get(f"/admin/tournaments/{imported}").text

    assert "Imported from wybin" in page
    assert "Add a stage" not in page and "Remove" not in page
    assert "NM1" in page and "HD1" in page and "Synth Artist - First Song [Rain]" in page
    assert (
        "The pool for this stage is not published yet. It will appear here automatically." in page
    )


def test_an_archived_manual_tournament_is_read_only(client, as_admin, db, tid):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (tid,))

    page = client.get(f"/admin/tournaments/{tid}").text

    assert "This tournament is archived" in page
    assert "Add a stage" not in page


def test_an_unknown_tournament_is_not_found(client, as_admin):
    assert client.get("/admin/tournaments/9999").status_code == 404


# --- stages ---


def test_a_stage_is_stored_with_utc_times_and_its_format(client, as_admin, db, tid):
    response = add_stage(
        client,
        as_admin,
        tid,
        start_at="2026-12-05T12:00",
        end_at="2026-12-06T09:30",
        best_of="9",
        bans="1",
        protects="2",
        is_qualifier="1",
    )

    row = stage_row(db, tid)
    assert (
        response.status_code == 303 and response.headers["location"] == f"/admin/tournaments/{tid}"
    )
    assert (row["start_at"], row["end_at"]) == ("2026-12-05T12:00:00Z", "2026-12-06T09:30:00Z")
    assert (row["best_of"], row["bans"], row["protects"], row["is_qualifier"]) == (9, 1, 2, 1)
    assert row["wybin_stage_id"] is None and row["lineup_size"] is None
    assert "Stage Round of 16 added." in client.get(f"/admin/tournaments/{tid}").text


def test_only_the_name_is_required_and_stages_keep_their_creation_order(client, as_admin, db, tid):
    add_stage(client, as_admin, tid, name="Qualifiers")
    add_stage(client, as_admin, tid, name="Finals")

    rows = db.execute(
        "SELECT name, sort_order, best_of, is_qualifier FROM stages ORDER BY sort_order"
    ).fetchall()

    assert [tuple(row) for row in rows] == [("Qualifiers", 1, None, 0), ("Finals", 2, None, 0)]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": " "}, "Enter a name for the stage"),
        ({"name": "x" * 61}, "Keep this under 60 characters."),
        ({"start_at": "tomorrow"}, "Enter a date and time."),
        (
            {"start_at": "2026-12-06T12:00", "end_at": "2026-12-05T12:00"},
            "cannot end before it starts",
        ),
        ({"best_of": "8"}, "Best of must be an odd number"),
        ({"best_of": "23"}, "Best of must be a whole number from 1 to 21."),
        ({"best_of": "nine"}, "Best of must be a whole number from 1 to 21."),
        ({"bans": "6"}, "Bans must be a whole number from 0 to 5."),
        ({"protects": "-1"}, "Protects must be a whole number from 0 to 5."),
    ],
)
def test_an_invalid_stage_is_refused_with_a_message_and_the_input_is_kept(
    client, as_admin, db, tid, overrides, message
):
    response = add_stage(client, as_admin, tid, **{"name": "Cup", **overrides})

    assert response.status_code == 422
    assert message in response.text
    assert db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 0
    for key, value in overrides.items():
        if value.strip() and key != "name":
            assert f'value="{value}"' in response.text


def test_stages_cannot_be_added_to_wybin_or_archived_tournaments(client, as_admin, db, tid, wybin):
    wybin.load_defaults()
    client.post("/admin/tournaments/import", data={**as_admin, "link": "synthetic-solo"})
    imported = db.execute("SELECT id FROM tournaments WHERE wybin_id = 9002").fetchone()[0]
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (tid,))

    from_wybin = add_stage(client, as_admin, imported)
    archived = add_stage(client, as_admin, tid)

    assert from_wybin.status_code == 409 and "come from wybin" in from_wybin.text
    assert archived.status_code == 409 and "archived" in archived.text
    assert db.execute("SELECT COUNT(*) FROM stages WHERE wybin_stage_id IS NULL").fetchone()[0] == 0


def test_removing_a_stage_removes_its_maps(client, as_admin, db, tid, osu):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid)
    add_map(client, as_admin, tid, 1)

    response = post(client, f"/admin/tournaments/{tid}/stages/1/remove", token=as_admin)

    assert response.status_code == 303
    assert db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0


def test_a_stage_of_another_tournament_cannot_be_removed_through_this_one(
    client, as_admin, db, tid
):
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_stage(client, as_admin, other)

    response = post(client, f"/admin/tournaments/{tid}/stages/1/remove", token=as_admin)

    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 1


# --- maps ---


def test_a_map_takes_its_details_from_osu_and_its_mods_from_the_slot(
    client, as_admin, db, tid, osu
):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid)

    response = add_map(
        client, as_admin, tid, 1, slot=" hd2 ", beatmap="https://osu.ppy.sh/b/129891"
    )

    row = db.execute("SELECT * FROM pool_maps").fetchone()
    assert response.status_code == 303
    assert (row["slot"], row["bracket"], row["required_mods"]) == ("HD2", "Hidden", "HD")
    assert (row["is_freemod"], row["is_tiebreaker"], row["sort_order"]) == (0, 0, 1)
    assert (row["beatmap_id"], row["beatmapset_id"]) == (129891, 130891)
    assert (row["artist"], row["title"], row["version"], row["mapper"]) == (
        "Synth Artist",
        "Song",
        "Insane",
        "MapperMia",
    )
    assert (row["star_rating"], row["bpm"], row["length_s"]) == (6.25, 180.0, 200)
    assert (row["cs"], row["ar"], row["od"], row["hp"]) == (4.0, 9.0, 8.5, 5.0)
    assert db.execute("SELECT pool_released FROM stages").fetchone()[0] == 1
    assert "Map HD2 added." in client.get(f"/admin/tournaments/{tid}").text


@pytest.mark.parametrize(
    ("slot", "mods", "free", "tiebreaker"),
    [
        ("NM1", "", 0, 0),
        ("FM2", "", 1, 0),
        ("TB", "", 0, 1),
        ("DT1", "DT", 0, 0),
        ("HDHR1", "HDHR", 0, 0),
    ],
)
def test_slot_prefixes_decide_mods_and_flags(
    client, as_admin, db, tid, osu, slot, mods, free, tiebreaker
):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid)

    add_map(client, as_admin, tid, 1, slot=slot)

    row = db.execute("SELECT required_mods, is_freemod, is_tiebreaker FROM pool_maps").fetchone()
    assert tuple(row) == (mods, free, tiebreaker)


def test_maps_are_listed_in_the_order_they_were_added(client, as_admin, db, tid, osu):
    for beatmap_id in (1, 2, 3):
        osu.add_beatmap(beatmap_id)
    add_stage(client, as_admin, tid)
    for slot, beatmap_id in (("HD1", 2), ("NM1", 1), ("NM2", 3)):
        add_map(client, as_admin, tid, 1, slot, str(beatmap_id))

    slots = [row[0] for row in db.execute("SELECT slot FROM pool_maps ORDER BY sort_order")]

    assert slots == ["HD1", "NM1", "NM2"]


@pytest.mark.parametrize(
    ("slot", "beatmap", "message"),
    [
        ("", "129891", "Use a slot like NM1"),
        ("N", "129891", "Use a slot like NM1"),
        ("NM1x", "129891", "Use a slot like NM1"),
        ("NM123", "129891", "Use a slot like NM1"),
        ("ZZ1", "129891", "ZZ is not a mod combination this game mode has."),
        ("NM1", "", "Enter a beatmap link or id"),
        ("NM1", "nonsense", "Enter a beatmap link or id"),
        ("NM1", "0", "Enter a beatmap link or id"),
        ("NM1", "99999999999", "Enter a beatmap link or id"),
    ],
)
def test_an_invalid_map_is_refused_and_the_input_kept(
    client, as_admin, db, tid, osu, slot, beatmap, message
):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid)

    response = add_map(client, as_admin, tid, 1, slot=slot, beatmap=beatmap)

    assert response.status_code == 422
    assert message in response.text
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0
    if slot:
        assert f'value="{slot}"' in response.text
    assert osu.calls("GET", "/beatmaps/129891") == []  # nothing is looked up for a bad form


def test_a_slot_that_needs_a_mod_the_mode_lacks_is_refused(client, as_admin, db, osu):
    client.post("/admin/tournaments", data={**as_admin, "name": "Mania Cup", "gamemode": "3"})
    mania = db.execute("SELECT id FROM tournaments WHERE name = 'Mania Cup'").fetchone()[0]
    osu.add_beatmap(129891)
    add_stage(client, as_admin, mania)

    hard_rock = add_map(client, as_admin, mania, 1, slot="HR1")
    hidden = add_map(client, as_admin, mania, 1, slot="HD1")

    assert hard_rock.status_code == 422 and "HR is not a mod combination" in hard_rock.text
    assert hidden.status_code == 303


def test_a_taken_slot_is_a_conflict(client, as_admin, db, tid, osu):
    osu.add_beatmap(129891)
    osu.add_beatmap(129892)
    add_stage(client, as_admin, tid)
    add_map(client, as_admin, tid, 1, slot="NM1")

    response = add_map(client, as_admin, tid, 1, slot="nm1", beatmap="129892")

    assert response.status_code == 409
    assert "The slot NM1 is already used in this stage." in response.text
    assert db.execute("SELECT beatmap_id FROM pool_maps").fetchone()[0] == 129891


def test_the_same_slot_can_exist_in_another_stage(client, as_admin, db, tid, osu):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid, name="Qualifiers")
    add_stage(client, as_admin, tid, name="Finals")

    add_map(client, as_admin, tid, 1)
    response = add_map(client, as_admin, tid, 2)

    assert response.status_code == 303
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 2


def test_an_unknown_beatmap_shows_an_inline_error(client, as_admin, db, tid):
    add_stage(client, as_admin, tid)

    response = add_map(client, as_admin, tid, 1, beatmap="555")

    assert response.status_code == 422
    assert "No beatmap with this id exists on osu!." in response.text
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0


def test_when_osu_is_down_the_map_is_not_added_and_the_page_says_why(
    client, as_admin, db, tid, osu
):
    osu.unavailable_paths.add("/api/v2/beatmaps/129891")
    add_stage(client, as_admin, tid)

    response = add_map(client, as_admin, tid, 1)

    assert response.status_code == 502
    assert "osu! did not answer, so the map could not be looked up." in response.text
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0


def test_a_map_needs_a_stage_of_this_tournament(client, as_admin, db, tid, osu):
    osu.add_beatmap(129891)
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_stage(client, as_admin, other)
    add_stage(client, as_admin, tid, name="Own stage")

    foreign = add_map(client, as_admin, tid, 1)
    missing = post(
        client, f"/admin/tournaments/{tid}/maps", {"slot": "NM1", "beatmap": "129891"}, as_admin
    )

    assert foreign.status_code == 404
    assert missing.status_code == 422 and "Choose the stage" in missing.text
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0


def test_maps_cannot_be_added_to_archived_tournaments(client, as_admin, db, tid, osu):
    osu.add_beatmap(129891)
    add_stage(client, as_admin, tid)
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (tid,))

    response = add_map(client, as_admin, tid, 1)

    assert response.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0


def test_removing_the_last_map_unpublishes_the_pool(client, as_admin, db, tid, osu):
    osu.add_beatmap(129891)
    osu.add_beatmap(129892)
    add_stage(client, as_admin, tid)
    add_map(client, as_admin, tid, 1, "NM1", "129891")
    add_map(client, as_admin, tid, 1, "NM2", "129892")

    post(client, f"/admin/tournaments/{tid}/maps/1/remove", token=as_admin)
    assert db.execute("SELECT pool_released FROM stages").fetchone()[0] == 1
    response = post(client, f"/admin/tournaments/{tid}/maps/2/remove", token=as_admin)

    assert response.status_code == 303
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 0
    assert db.execute("SELECT pool_released FROM stages").fetchone()[0] == 0


def test_a_map_of_another_tournament_cannot_be_removed_through_this_one(
    client, as_admin, db, tid, osu
):
    osu.add_beatmap(129891)
    other = add_tournament(db, "Other Cup", acronym="OC")
    add_stage(client, as_admin, other)
    add_map(client, as_admin, other, 1)

    response = post(client, f"/admin/tournaments/{tid}/maps/1/remove", token=as_admin)

    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) FROM pool_maps").fetchone()[0] == 1
    assert (
        post(client, f"/admin/tournaments/{tid}/maps/99/remove", token=as_admin).status_code == 404
    )
