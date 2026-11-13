"""Importing from a public Google Sheets link: only docs.google.com is ever contacted (D7)."""

import httpx
import pytest

from app.clients.http import make_http_client
from app.services.csv_text import MAX_CSV_BYTES, CsvImportError
from app.services.sheets_link import MAX_REDIRECTS, export_url, fetch_sheet_csv
from tests.support import (
    SHEET_ID,
    add_member,
    add_pool_map,
    add_stage,
    add_tournament,
)

CAPTAIN, PLAYER = 2001, 2002
BASE = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
SHEET_CSV = "slot,player,score,accuracy\nNM1,player2002,900000,98.5\nHD1,player2002,800000,97\n"


# --- which links are accepted ---


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (f"{BASE}/edit", f"{BASE}/export?format=csv"),
        (f"{BASE}/edit#gid=0", f"{BASE}/export?format=csv&gid=0"),
        (f"{BASE}/edit#gid=1234567", f"{BASE}/export?format=csv&gid=1234567"),
        (f"{BASE}/edit?usp=sharing&gid=42", f"{BASE}/export?format=csv&gid=42"),
        (f"  {BASE}  ", f"{BASE}/export?format=csv"),
        (f"{BASE}/export?format=xlsx", f"{BASE}/export?format=csv"),  # rebuilt, not copied
        (f"https://DOCS.GOOGLE.COM/spreadsheets/d/{SHEET_ID}/edit", f"{BASE}/export?format=csv"),
    ],
)
def test_the_export_address_is_rebuilt_from_the_sheet_id_and_tab(link, expected):
    assert export_url(link) == expected


@pytest.mark.parametrize(
    "link",
    [
        "",
        "not a link",
        f"http://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",  # not https
        f"https://drive.google.com/spreadsheets/d/{SHEET_ID}/edit",  # another Google host
        f"https://example.com/spreadsheets/d/{SHEET_ID}/edit",
        f"https://docs.google.com.evil.example/spreadsheets/d/{SHEET_ID}/edit",
        f"https://evil.example/docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        f"https://docs.google.com@evil.example/spreadsheets/d/{SHEET_ID}/edit",
        f"https://docs.google.com:8443@evil.example/spreadsheets/d/{SHEET_ID}/edit",
        f"https://user:pw@docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        f"https://docs.google.com:8443/spreadsheets/d/{SHEET_ID}/edit",
        f"https://docs.google.com:abc/spreadsheets/d/{SHEET_ID}/edit",
        "https://docs.google.com./spreadsheets/d/" + SHEET_ID,
        "https://docs.google.com/document/d/" + SHEET_ID + "/edit",
        "https://docs.google.com/spreadsheets/d/short/edit",  # not a sheet id
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}x%2F..%2F/edit",
        f"https://docs.google.com/spreadsheets/u/0/d/{SHEET_ID}/edit",
        f"{BASE}/edit#gid=abc",
        f"{BASE}/edit?gid=1;2",
        f"{BASE}/edit?gid=12345678901",  # more digits than a tab id has
        f"ftp://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        f"//docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
    ],
)
def test_anything_but_a_google_sheets_link_is_refused(link):
    with pytest.raises(CsvImportError, match="Paste the link of a Google Sheet"):
        export_url(link)


# --- fetching ---


@pytest.fixture
def fetch(sheets):
    http = make_http_client(sheets.transport)

    async def run(link: str = f"{BASE}/edit#gid=0") -> str:
        return await fetch_sheet_csv(http, link)

    return run


@pytest.mark.anyio
async def test_the_sheet_is_fetched_from_docs_google_com_as_csv(fetch, sheets):
    sheets.sheets[SHEET_ID] = SHEET_CSV

    assert await fetch() == SHEET_CSV

    (request,) = sheets.requests
    assert request.method == "GET" and request.url.host == "docs.google.com"
    assert request.url.scheme == "https"
    assert sheets.paths() == [f"/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"]
    assert "authorization" not in request.headers and "cookie" not in request.headers


@pytest.mark.anyio
async def test_a_refused_link_makes_no_request_at_all(fetch, sheets):
    with pytest.raises(CsvImportError):
        await fetch("https://evil.example/spreadsheets/d/" + SHEET_ID)

    assert sheets.requests == []


@pytest.mark.anyio
async def test_a_byte_order_mark_is_dropped(fetch, sheets):
    sheets.sheets[SHEET_ID] = "﻿" + SHEET_CSV

    assert await fetch() == SHEET_CSV


@pytest.mark.anyio
async def test_a_redirect_inside_docs_google_com_is_followed(fetch, sheets):
    sheets.sheets[SHEET_ID] = SHEET_CSV
    sheets.redirect_once_to = f"{BASE}/export?format=csv&gid=7"

    assert await fetch() == SHEET_CSV

    assert sheets.paths() == [
        f"/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0",
        f"/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=7",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "target",
    [
        "https://accounts.google.com/ServiceLogin?continue=x",
        "https://evil.example/export.csv",
        f"http://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv",
        "http://169.254.169.254/latest/meta-data/",
        "https://docs.google.com.evil.example/x",
        "//evil.example/x",
    ],
)
async def test_a_redirect_to_any_other_host_is_not_followed(fetch, sheets, target):
    sheets.redirect_to = target

    with pytest.raises(CsvImportError, match="did not hand out the sheet"):
        await fetch()

    assert len(sheets.requests) == 1  # nothing beyond the first request left


@pytest.mark.anyio
async def test_endless_redirects_stop(fetch, sheets):
    sheets.redirect_to = f"{BASE}/export?format=csv"

    with pytest.raises(CsvImportError, match="did not hand out the sheet"):
        await fetch()

    assert len(sheets.requests) == MAX_REDIRECTS + 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "did not hand out the sheet"),
        (403, "did not hand out the sheet"),
        (404, "could not find this sheet"),
        (500, "did not answer"),
        (429, "did not answer"),
    ],
)
async def test_google_refusals_are_explained(fetch, sheets, status, message):
    sheets.status = status

    with pytest.raises(CsvImportError, match=message):
        await fetch()


@pytest.mark.anyio
async def test_a_sign_in_page_instead_of_csv_means_the_sheet_is_private(fetch, sheets):
    sheets.sheets[SHEET_ID] = "<html>Sign in</html>"
    sheets.content_type = "text/html; charset=utf-8"

    with pytest.raises(CsvImportError, match="Anyone with the link"):
        await fetch()


@pytest.mark.anyio
async def test_an_unknown_sheet_is_not_found(fetch):
    with pytest.raises(CsvImportError, match="could not find this sheet"):
        await fetch()


@pytest.mark.anyio
async def test_a_sheet_over_one_megabyte_is_refused(fetch, sheets):
    sheets.sheets[SHEET_ID] = "a" * (MAX_CSV_BYTES + 1)

    with pytest.raises(CsvImportError, match="larger than 1 MB"):
        await fetch()


@pytest.mark.anyio
async def test_a_sheet_of_exactly_one_megabyte_is_taken(fetch, sheets):
    sheets.sheets[SHEET_ID] = "a" * MAX_CSV_BYTES

    assert len(await fetch()) == MAX_CSV_BYTES


@pytest.mark.anyio
async def test_a_sheet_that_is_not_utf8_is_refused(fetch, sheets):
    http = make_http_client(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"slot\xff\xfe\n"))
    )

    with pytest.raises(CsvImportError, match="not UTF-8"):
        await fetch_sheet_csv(http, f"{BASE}/edit")


@pytest.mark.anyio
async def test_a_network_failure_is_reported_without_details():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure for secret.internal")

    with pytest.raises(CsvImportError, match="Google did not answer") as caught:
        await fetch_sheet_csv(make_http_client(httpx.MockTransport(broken)), f"{BASE}/edit")

    assert "secret.internal" not in str(caught.value)


# --- through the import page ---


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    stage = add_stage(db, tid)
    add_pool_map(db, stage, "NM1")
    add_pool_map(db, stage, "HD1", required_mods="HD")
    return {"id": tid, "stage": stage}


def post_link(client, token, tid, link, **extra):
    return client.post(f"/t/{tid}/import", data={**token, "link": link, **extra})


def test_a_public_sheet_is_previewed_and_imported_like_a_file(client, login, csrf, db, sheets, cup):
    sheets.sheets[SHEET_ID] = SHEET_CSV
    login(CAPTAIN)
    token = csrf()

    preview = post_link(client, token, cup["id"], f"{BASE}/edit#gid=0")

    assert preview.status_code == 200 and "2 new" in preview.text
    assert sheets.paths() == [f"/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"]
    fields = {
        "csv_text": SHEET_CSV,
        "stage": str(cup["stage"]),
        "map_slot": "0",
        "map_player": "1",
        "map_score": "2",
        "map_accuracy": "3",
    }
    done = client.post(f"/t/{cup['id']}/import/commit", data={**token, **fields})
    assert "Imported 2 scores." in done.text
    sources = db.execute("SELECT source, score FROM scores ORDER BY id").fetchall()
    assert [tuple(row) for row in sources] == [("import", 900000), ("import", 800000)]


@pytest.mark.parametrize(
    "link",
    [
        "https://example.com/spreadsheets/d/" + SHEET_ID,
        f"http://docs.google.com/spreadsheets/d/{SHEET_ID}",
        f"https://docs.google.com@evil.example/spreadsheets/d/{SHEET_ID}",
        "file:///etc/passwd",
        "http://127.0.0.1:8080/healthz",
        "not a link",
    ],
)
def test_a_link_to_anywhere_else_is_refused_before_anything_is_fetched(
    client, login, csrf, db, sheets, cup, link
):
    login(CAPTAIN)

    response = post_link(client, csrf(), cup["id"], link)

    assert response.status_code == 422 and "Paste the link of a Google Sheet" in response.text
    assert sheets.requests == []
    assert db.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


def test_a_private_sheet_tells_the_captain_how_to_share_it(client, login, csrf, sheets, cup):
    sheets.status = 403
    login(CAPTAIN)

    response = post_link(client, csrf(), cup["id"], f"{BASE}/edit")

    assert response.status_code == 422 and "Anyone with the link" in response.text
    assert f'value="{BASE}/edit"' in response.text  # what was pasted stays in the field


def test_a_sheet_redirecting_away_is_refused(client, login, csrf, sheets, cup):
    sheets.redirect_to = "https://evil.example/steal"
    login(CAPTAIN)

    response = post_link(client, csrf(), cup["id"], f"{BASE}/edit")

    assert response.status_code == 422
    assert {request.url.host for request in sheets.requests} == {"docs.google.com"}


def test_a_file_wins_over_a_link_when_both_are_sent(client, login, csrf, sheets, cup):
    login(CAPTAIN)

    response = client.post(
        f"/t/{cup['id']}/import",
        data={**csrf(), "link": f"{BASE}/edit"},
        files={"file": ("s.csv", SHEET_CSV.encode(), "text/csv")},
    )

    assert response.status_code == 200 and "2 new" in response.text
    assert sheets.requests == []
