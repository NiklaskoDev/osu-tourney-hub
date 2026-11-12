import pytest

from tests.support import ADMIN_ID, add_member, add_tournament


@pytest.fixture
def cup(client, login, csrf, db, wybin) -> int:
    wybin.load_defaults()
    login(ADMIN_ID, "AdminAmy")
    client.post("/admin/tournaments/import", data={**csrf(), "link": "synthetic-cup"})
    return db.execute("SELECT id FROM tournaments WHERE wybin_id = 9001").fetchone()[0]


def test_the_overview_shows_mode_format_team_and_stages(client, login, cup):
    login(3002)

    page = client.get(f"/t/{cup}").text

    assert "osu!catch" in page
    assert "Teams of 3 to 6" in page
    assert "Alpha Squad" in page
    assert "Last synced from wybin" in page
    for stage in ("Qualifiers", "Round of 16", "Finals"):
        assert stage in page
    assert "Best of 9, 1 ban" in page
    assert "Best of 13, 2 bans, 1 protect" in page
    assert page.index("Qualifiers") < page.index("Round of 16") < page.index("Finals")


def test_the_overview_says_which_pools_are_published(client, login, cup):
    login(3002)

    page = client.get(f"/t/{cup}").text

    assert page.count("Published") == 1  # only Qualifiers has real maps
    assert page.count("Not published yet") == 2


def test_the_overview_shows_the_final_placing_once_set(client, login, db, cup):
    login(3002)
    assert "Final placing" not in client.get(f"/t/{cup}").text
    db.execute("UPDATE tournaments SET final_placing = 'Top 8' WHERE id = ?", (cup,))

    assert "Top 8" in client.get(f"/t/{cup}").text


def test_a_solo_tournament_is_described_as_solo(client, login, csrf, db, wybin):
    wybin.load_defaults()
    login(ADMIN_ID, "AdminAmy")
    client.post("/admin/tournaments/import", data={**csrf(), "link": "synthetic-solo"})
    solo = db.execute("SELECT id FROM tournaments").fetchone()[0]

    page = client.get(f"/t/{solo}").text

    assert "Solo" in page and "Your entry" in page and "osu!" in page


def test_a_tournament_without_stages_or_team_has_empty_states(client, login, db):
    tid = add_tournament(db, "Bare Cup", acronym="BC")
    add_member(db, tid, 2002, "player")
    login(2002)

    page = client.get(f"/t/{tid}").text

    assert "This tournament has no stages yet." in page
    assert "They appear here after the next wybin sync." not in page  # not from wybin
    assert "Not set" in page


def test_an_empty_wybin_tournament_says_stages_follow_with_the_next_sync(client, login, db):
    tid = add_tournament(db, "Wybin Cup", acronym="WC")
    db.execute("UPDATE tournaments SET source = 'wybin' WHERE id = ?", (tid,))
    add_member(db, tid, 2002, "player")
    login(2002)

    assert "They appear here after the next wybin sync." in client.get(f"/t/{tid}").text


def test_the_admin_dashboard_offers_adding_a_tournament_and_members_do_not(client, login, db):
    tid = add_tournament(db)
    add_member(db, tid, 2002, "player")
    login(ADMIN_ID)
    assert 'href="/admin"' in client.get("/").text
    assert "Add tournament" in client.get("/").text
    login(2002)

    assert "Add tournament" not in client.get("/").text
