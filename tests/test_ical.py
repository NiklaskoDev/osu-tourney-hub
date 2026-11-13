"""The iCalendar text, and the feed endpoint with its tokens."""

import hashlib
import logging
import re
from datetime import UTC, datetime

import pytest

from app.services import feed_tokens, ical
from app.services.ical import CalendarEvent, build_calendar, escape_text, fold_line
from app.web.security import FEED_PATH_REDACTED, RedactFeedTokens
from tests.support import (
    ADMIN_ID,
    add_match,
    add_member,
    add_team,
    add_tournament,
)

CAPTAIN, PLAYER, OTHER = 2001, 2002, 2009


def event(match_id=7, summary="[SC] R16: Alpha vs Rival") -> CalendarEvent:
    return CalendarEvent(
        match_id=match_id,
        start=datetime(2026, 12, 6, 12, 0, tzinfo=UTC),
        summary=summary,
        modified=datetime(2026, 11, 1, 8, 30, 5, tzinfo=UTC),
    )


def unfold(text: str) -> list[str]:
    """Content lines as a calendar app reads them: continuation lines rejoined."""
    return text.replace("\r\n ", "").split("\r\n")[:-1]


# --- text escaping and folding -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("plain", "plain"),
        ("a,b", "a\\,b"),
        ("a;b", "a\\;b"),
        ("back\\slash", "back\\\\slash"),
        ("two\nlines", "two\\nlines"),
        ("crlf\r\nline", "crlf\\nline"),
        ("lone\rcr", "lone\\ncr"),
        ("bell\x07 and nul\x00", "bell and nul"),
        ("tab\there", "tabhere"),
        ("Ünïcode ✓", "Ünïcode ✓"),
    ],
)
def test_text_values_are_escaped_per_the_rfc(raw, escaped):
    assert escape_text(raw) == escaped


def test_a_short_line_is_not_folded():
    assert fold_line("SUMMARY:short") == "SUMMARY:short"


def test_a_long_line_is_folded_at_75_octets_with_a_leading_space():
    folded = fold_line("SUMMARY:" + "x" * 200)

    pieces = folded.split("\r\n")
    assert all(len(piece.encode()) <= 75 for piece in pieces)
    assert all(piece.startswith(" ") for piece in pieces[1:])
    assert folded.replace("\r\n ", "") == "SUMMARY:" + "x" * 200


def test_folding_counts_octets_not_characters_and_never_splits_a_character():
    line = "SUMMARY:" + "é" * 80 + "✓" * 30 + "😀" * 10  # 2, 3 and 4 octet characters

    folded = fold_line(line)

    pieces = folded.split("\r\n")
    assert all(len(piece.encode()) <= 75 for piece in pieces)
    for piece in pieces:
        piece.encode("utf-8").decode("utf-8")  # every piece is valid UTF-8 on its own
    assert folded.replace("\r\n ", "") == line


def test_a_line_of_exactly_75_octets_stays_whole():
    line = "X" * 75

    assert fold_line(line) == line
    assert fold_line(line + "Y") == line + "\r\n Y"


# --- the calendar ------------------------------------------------------------------------------


def test_the_calendar_is_a_vcalendar_with_crlf_line_ends_and_no_bare_newlines():
    text = build_calendar([event()], "Hub")

    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert "\r" not in text.replace("\r\n", "")


def test_the_header_names_the_version_product_and_calendar():
    lines = unfold(build_calendar([], "osu! tourney hub matches"))

    assert lines == [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//osu-tourney-hub//Match schedule//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:osu! tourney hub matches",
        "END:VCALENDAR",
    ]


def test_an_event_has_a_stable_uid_utc_times_and_the_default_length():
    lines = unfold(build_calendar([event(match_id=42)], "Hub"))

    assert "UID:match-42@osu-tourney-hub" in lines
    assert "DTSTART:20261206T120000Z" in lines
    assert "DTEND:20261206T133000Z" in lines  # 1.5 hours later
    assert "DTSTAMP:20261101T083005Z" in lines  # when the match last changed, not "now"
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 1


def test_times_in_other_zones_are_written_in_utc():
    from datetime import timedelta, timezone

    berlin = timezone(timedelta(hours=1))
    local = CalendarEvent(
        1, datetime(2026, 12, 6, 13, 0, tzinfo=berlin), "x", datetime(2026, 11, 1, tzinfo=berlin)
    )

    assert "DTSTART:20261206T120000Z" in unfold(build_calendar([local], "Hub"))


def test_the_summary_is_escaped_and_long_ones_are_folded_within_the_limit():
    tricky = "[SC] Semi; final, Café " + "very long name " * 12
    text = build_calendar([event(summary=tricky)], "Hub")

    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    summary = next(line for line in unfold(text) if line.startswith("SUMMARY:"))
    assert summary.startswith("SUMMARY:[SC] Semi\\; final\\, Café very long name")


def test_every_match_gets_its_own_event():
    text = build_calendar([event(1), event(2), event(3)], "Hub")

    assert re.findall(r"UID:(match-\d+@osu-tourney-hub)", text) == [
        "match-1@osu-tourney-hub",
        "match-2@osu-tourney-hub",
        "match-3@osu-tourney-hub",
    ]


def test_an_empty_feed_is_still_a_valid_calendar():
    text = build_calendar([], "Hub")

    assert "BEGIN:VEVENT" not in text and text.endswith("END:VCALENDAR\r\n")


def test_a_hostile_name_cannot_inject_calendar_lines():
    text = build_calendar([event(summary="x\r\nBEGIN:VEVENT\r\nATTENDEE:evil")], "Hub")

    assert unfold(text).count("BEGIN:VEVENT") == 1  # the text stays inside the SUMMARY line
    assert not any(line.startswith("ATTENDEE") for line in unfold(text))


def test_the_default_match_length_is_a_named_constant():
    assert ical.MATCH_DURATION.total_seconds() == 90 * 60


# --- tokens ------------------------------------------------------------------------------------


def test_tokens_are_long_random_and_url_safe():
    tokens = {feed_tokens.new_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", token) for token in tokens)


def test_only_a_sha256_of_the_token_is_kept():
    assert feed_tokens.hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


# --- the endpoint ------------------------------------------------------------------------------


@pytest.fixture
def cup(db):
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, PLAYER, "player")
    ours = add_team(db, tid, "Alpha Squad", [(CAPTAIN, "c")], ours=True)
    rival = add_team(db, tid, "Rival Crew", [(4001, "r")])
    third = add_team(db, tid, "Third Wheel", [(5001, "t")])
    return {"id": tid, "ours": ours, "rival": rival, "third": third}


def make_link(client, login, csrf, osu_id) -> str:
    login(osu_id)
    page = client.post("/me/feed-token", data=csrf()).text
    return re.search(r'value="(https://[^"]+\.ics)"', page).group(1)


def path_of(link: str) -> str:
    return "/" + link.split("/", 3)[3]


def test_creating_a_link_shows_it_once_and_stores_only_its_hash(client, login, csrf, db):
    link = make_link(client, login, csrf, CAPTAIN)

    token = link.rsplit("/", 1)[1].removesuffix(".ics")
    assert link == f"https://tourney.example.com/feeds/{token}.ics"
    row = db.execute("SELECT token_hash FROM feed_tokens WHERE osu_id = ?", (CAPTAIN,)).fetchone()
    assert row["token_hash"] == feed_tokens.hash_token(token)
    assert token not in " ".join(str(tuple(r)) for r in db.execute("SELECT * FROM feed_tokens"))
    assert token not in client.get("/me").text  # the profile never shows it again
    assert "cannot be shown again" in client.get("/me").text


def test_the_page_that_shows_the_link_is_not_cached(client, login, csrf):
    login(PLAYER)

    response = client.post("/me/feed-token", data=csrf())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_the_link_is_kept_out_of_the_session_cookie(client, login, csrf):
    link = make_link(client, login, csrf, CAPTAIN)
    token = link.rsplit("/", 1)[1].removesuffix(".ics")

    assert token not in "".join(client.cookies.values())


def test_the_feed_lists_our_matches_as_calendar_events(client, login, csrf, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-12-06T12:00:00Z", None, "R16 M1")
    add_match(db, cup["id"], cup["third"], cup["ours"], "2026-12-07T18:30:00Z", None, "R16 M2")
    link = make_link(client, login, csrf, PLAYER)
    client.cookies.clear()  # a calendar app has no session

    response = client.get(path_of(link))

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.headers["cache-control"] == "private, no-store"
    lines = unfold(response.text)
    assert lines.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:[SC] R16 M1: Alpha Squad vs Rival Crew" in lines
    assert "SUMMARY:[SC] R16 M2: Third Wheel vs Alpha Squad" in lines
    assert "DTSTART:20261206T120000Z" in lines and "DTSTART:20261207T183000Z" in lines
    assert response.text.endswith("\r\n")


def test_the_feed_leaves_out_other_teams_and_unscheduled_matches(client, login, csrf, cup, db):
    add_match(db, cup["id"], cup["rival"], cup["third"], "2026-12-06T12:00:00Z", None, "Not ours")
    add_match(db, cup["id"], cup["ours"], cup["rival"], None, None, "No time")
    link = make_link(client, login, csrf, PLAYER)

    assert "BEGIN:VEVENT" not in client.get(path_of(link)).text


def test_the_feed_only_has_tournaments_the_person_belongs_to(client, login, csrf, cup, db):
    elsewhere = add_tournament(db, "Other Cup", acronym="OC")
    mine = add_team(db, elsewhere, "Mine", [(OTHER, "o")], ours=True)
    other = add_team(db, elsewhere, "Theirs")
    add_match(db, elsewhere, mine, other, "2026-12-09T12:00:00Z", None, "Hidden from you")
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-12-06T12:00:00Z", None, "Visible")
    link = make_link(client, login, csrf, PLAYER)

    feed = client.get(path_of(link)).text

    assert "Visible" in feed and "Hidden from you" not in feed


def test_the_feed_skips_archived_and_hidden_tournaments(client, login, csrf, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-12-06T12:00:00Z", None, "Old")
    link = make_link(client, login, csrf, PLAYER)
    for status in ("archived", "hidden"):
        db.execute("UPDATE tournaments SET status = ? WHERE id = ?", (status, cup["id"]))

        assert "BEGIN:VEVENT" not in client.get(path_of(link)).text


def test_an_unknown_token_is_a_plain_not_found(client, cup):
    response = client.get("/feeds/" + "a" * 43 + ".ics")

    assert response.status_code == 404
    assert "text/calendar" not in response.headers["content-type"]


@pytest.mark.parametrize("token", ["x", "../../etc/passwd", "a" * 5000, "%00", "tokentoken.ics"])
def test_malformed_tokens_are_not_found(client, token):
    assert client.get(f"/feeds/{token}.ics").status_code == 404


def test_the_feed_needs_the_ics_ending(client, login, csrf):
    link = make_link(client, login, csrf, PLAYER)

    assert client.get(path_of(link).removesuffix(".ics")).status_code == 404


def test_a_revoked_link_stops_working(client, login, csrf, cup, db):
    link = make_link(client, login, csrf, PLAYER)
    assert client.get(path_of(link)).status_code == 200

    response = client.post("/me/feed-token/revoke", data=csrf(), follow_redirects=False)

    assert response.status_code == 303
    assert "no longer works" in client.get("/me").text
    assert client.get(path_of(link)).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM feed_tokens").fetchone()[0] == 0


def test_a_new_link_replaces_the_old_one(client, login, csrf, db):
    first = make_link(client, login, csrf, PLAYER)
    second = make_link(client, login, csrf, PLAYER)

    assert first != second
    assert client.get(path_of(first)).status_code == 404
    assert client.get(path_of(second)).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM feed_tokens").fetchone()[0] == 1


def test_each_person_has_their_own_link(client, login, csrf, cup, db):
    add_match(db, cup["id"], cup["ours"], cup["rival"], "2026-12-06T12:00:00Z", None, "Visible")
    mine = make_link(client, login, csrf, PLAYER)
    theirs = make_link(client, login, csrf, OTHER)

    assert "Visible" in client.get(path_of(mine)).text
    assert "Visible" not in client.get(path_of(theirs)).text


def test_creating_and_revoking_need_a_login_and_the_csrf_token(client, login, csrf):
    assert client.post("/me/feed-token", follow_redirects=False).status_code in (303, 401, 403)
    login(PLAYER)
    assert client.post("/me/feed-token").status_code == 403
    assert client.post("/me/feed-token/revoke").status_code == 403


def test_the_profile_offers_a_link_and_explains_what_it_shows(client, login):
    login(PLAYER)

    page = client.get("/me").text

    assert "Create calendar link" in page and "Anyone who has the link can read it" in page
    assert "Revoke link" not in page


def test_an_admin_without_matches_gets_an_empty_calendar(client, login, csrf):
    link = make_link(client, login, csrf, ADMIN_ID)

    assert "BEGIN:VEVENT" not in client.get(path_of(link)).text


# --- the access log ----------------------------------------------------------------------------


def log_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 200),
        None,
    )


def test_the_access_log_never_shows_a_feed_token():
    token = "S3cretTokenValue_-" + "x" * 25
    record = log_record(f"/feeds/{token}.ics")

    assert RedactFeedTokens().filter(record)

    message = record.getMessage()
    assert token not in message and FEED_PATH_REDACTED in message


def test_other_paths_are_logged_unchanged():
    record = log_record("/t/1/availability?week=2026-11-16")

    RedactFeedTokens().filter(record)

    assert "/t/1/availability?week=2026-11-16" in record.getMessage()


def test_the_app_installs_the_redaction_on_the_access_log(app):
    access_log = logging.getLogger("uvicorn.access")

    assert sum(isinstance(f, RedactFeedTokens) for f in access_log.filters) == 1
