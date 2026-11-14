"""The scanner's status numbers and the admin page that shows them."""

import dataclasses
import re
from datetime import timedelta

import pytest

from app.repos import app_settings as app_settings_repo
from app.repos import jobs as jobs_repo
from app.repos import scan as scan_repo
from app.services.skimmer import LAST_ERROR_KEY, Skimmer, scanner_status
from tests.support import ADMIN_ID, add_user

NOW_MINUTE = "2026-11-12T12:30"


def minute(clock, ago):
    return (clock.current - timedelta(minutes=ago)).strftime("%Y-%m-%dT%H:%M")


def seed(db, clock, ago_range, scanned=10, not_found=2, requests=20, head=None, step=5):
    """One counter row per minute for the given minutes ago; the head moves by `step` a minute."""
    ago_range = list(ago_range)
    for ago in ago_range:
        scan_repo.add_stats(
            db,
            minute(clock, ago),
            ids_scanned=scanned,
            not_found=not_found,
            requests=requests,
            head_id=None if head is None else head + step * (max(ago_range) - ago),
        )


def cursors(db, cursor, head):
    jobs_repo.set_cursor(db, "skimmer", str(cursor))
    jobs_repo.set_cursor(db, "skimmer_head", str(head))


def status(db, settings, clock, **changes):
    return scanner_status(db, dataclasses.replace(settings, **changes), clock.current)


# --- the numbers ---


def test_the_status_reports_cursor_head_and_backlog(db, settings, clock):
    cursors(db, 900, 1100)

    result = status(db, settings, clock)

    assert (result.cursor_id, result.head_id, result.backlog) == (900, 1100, 200)


def test_rates_come_from_the_counters_of_the_last_15_minutes_and_hour(db, settings, clock):
    seed(db, clock, range(0, 15), head=1000)  # 10 ids, 2 deleted, 20 requests a minute; head +5
    seed(db, clock, [30], scanned=60, not_found=6, requests=60, head=850, step=0)
    cursors(db, 900, 1100)

    result = status(db, settings, clock)

    assert result.created_per_min_15 == pytest.approx(5.0)
    assert result.scanned_per_min_15 == pytest.approx(10.0)
    assert result.requests_per_min_15 == pytest.approx(20.0)
    assert result.created_per_min_60 == pytest.approx((1070 - 850) / 30)
    assert result.scanned_per_min_60 == pytest.approx((150 + 60) / 31)  # counters span 31 minutes
    assert result.requests_per_min_60 == pytest.approx((300 + 60) / 31)
    assert result.not_found_rate_60 == pytest.approx((30 + 6) / 210)


def test_counters_older_than_the_window_are_left_out(db, settings, clock):
    seed(db, clock, [20], scanned=500)
    seed(db, clock, range(0, 3), scanned=10)

    result = status(db, settings, clock)

    assert result.scanned_per_min_15 == pytest.approx(10.0)  # three minutes of data, not fifteen
    assert result.scanned_per_min_60 == pytest.approx((500 + 30) / 21)


def test_without_any_counters_the_rates_are_empty(db, settings, clock):
    result = status(db, settings, clock)

    assert result.cursor_id is None and result.backlog is None
    assert result.scanned_per_min_15 is None and result.created_per_min_15 is None
    assert result.not_found_rate_60 is None
    assert (result.falling_behind, result.eta_minutes) == (False, None)


def test_a_catching_up_scanner_reports_how_long_it_needs(db, settings, clock):
    seed(db, clock, range(0, 15), scanned=10, head=1000, step=5)  # 5 created, 10 scanned
    cursors(db, 900, 1100)

    result = status(db, settings, clock)

    assert result.falling_behind is False
    assert result.eta_minutes == pytest.approx(200 / 5)


def test_a_scanner_that_is_not_keeping_up_is_falling_behind_and_has_no_estimate(
    db, settings, clock
):
    seed(db, clock, range(0, 15), scanned=10, head=1000, step=15)  # 15 created, 10 scanned
    cursors(db, 900, 1400)

    result = status(db, settings, clock)

    assert result.falling_behind is True
    assert result.eta_minutes is None


def test_a_scanner_that_is_caught_up_is_not_falling_behind(db, settings, clock):
    seed(db, clock, range(0, 15), scanned=10, head=1000, step=10)  # as many created as scanned
    cursors(db, 1135, 1140)

    assert status(db, settings, clock).falling_behind is False


def test_open_lobbies_gaps_and_the_last_error_are_part_of_the_status(db, settings, clock):
    scan_repo.upsert_open(db, 6, "2026-11-12T12:00:00Z", None, 0, "2026-11-12T12:00:00Z")
    scan_repo.upsert_open(db, 7, "2026-11-12T12:00:00Z", None, 0, "2026-11-12T12:00:00Z")
    scan_repo.insert_gap(db, 10, 20, "skipped_backlog", "2026-11-12T12:00:00Z")
    app_settings_repo.put(db, LAST_ERROR_KEY, "Lobby 9 could not be read after 3 tries.")

    result = status(db, settings, clock)

    assert result.open_lobbies == 2
    assert [(g.start_id, g.end_id) for g in result.gaps] == [(10, 20)]
    assert result.last_error == "Lobby 9 could not be read after 3 tries."


def test_a_failing_job_shows_as_the_last_error_when_no_lobby_failed(db, settings, clock):
    jobs_repo.record_start(db, "skimmer_head", "2026-11-12T12:00:00Z")
    jobs_repo.record_failure(db, "skimmer_head", "osu! could not be reached.")

    assert status(db, settings, clock).last_error == "osu! could not be reached."


def test_errors_of_the_last_hour_are_part_of_the_status(db, settings, clock):
    scan_repo.add_stats(db, minute(clock, 10), ids_scanned=3, errors=2)
    scan_repo.add_stats(db, minute(clock, 5), ids_scanned=3, errors=1)
    scan_repo.add_stats(db, minute(clock, 90), ids_scanned=3, errors=7)  # too long ago

    assert status(db, settings, clock).errors_60 == 3


def test_the_status_knows_about_the_switches(db, settings, clock):
    assert status(db, settings, clock).paused is False
    Skimmer.set_paused(db, True)

    result = status(db, settings, clock, scanner_enabled=False, scanner_budget_per_min=20)

    assert (result.paused, result.enabled, result.budget_per_min) == (True, False, 20)


def test_old_counters_are_pruned_after_24_hours(db, clock):
    scan_repo.add_stats(db, minute(clock, 25 * 60), ids_scanned=1)
    scan_repo.add_stats(db, minute(clock, 23 * 60), ids_scanned=1)

    scan_repo.prune_stats(db, minute(clock, 24 * 60))

    assert [r.minute for r in scan_repo.stats_since(db, "0")] == [minute(clock, 23 * 60)]


def test_adding_to_a_minute_adds_up_and_keeps_the_highest_head(db, clock):
    when = minute(clock, 0)
    scan_repo.add_stats(db, when, ids_scanned=1, requests=2, head_id=50)
    scan_repo.add_stats(db, when, ids_scanned=2, not_found=1, matches_with_hits=1, head_id=40)
    scan_repo.add_stats(db, when, requests=3)

    (row,) = scan_repo.stats_since(db, "0")
    assert (row.ids_scanned, row.not_found, row.matches_with_hits) == (3, 1, 1)
    assert (row.requests, row.head_id) == (5, 50)


# --- the page ---


@pytest.fixture
def as_admin(client, login, csrf, db):
    login(ADMIN_ID, "AdminAmy")
    return csrf()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/scanner"),
        ("get", "/admin/scanner/status"),
        ("post", "/admin/scanner/pause"),
        ("post", "/admin/scanner/resume"),
        ("post", "/admin/scanner/gaps/1/backfill"),
        ("post", "/admin/scanner/gaps/1/stop"),
        ("post", "/admin/scanner/start"),
    ],
)
def test_the_scanner_pages_are_for_admins_only(client, login, csrf, db, method, path):
    add_user(db, 2001, "Plain")
    login(2001, "Plain")
    data = csrf() if method == "post" else None
    response = getattr(client, method)(
        path, **({"data": data} if data else {}), follow_redirects=False
    )

    assert response.status_code == 403


def test_anonymous_visitors_are_sent_to_the_login(client):
    response = client.get("/admin/scanner", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


def test_scanner_posts_need_the_csrf_token(client, as_admin, db):
    response = client.post("/admin/scanner/pause")

    assert response.status_code == 403
    assert Skimmer.is_paused(db) is False


def test_starting_from_an_id_needs_the_csrf_token(client, as_admin, db):
    cursors(db, 1000, 1200)

    response = client.post("/admin/scanner/start", data={"start": "900"})

    assert response.status_code == 403 and scan_repo.list_gaps(db) == []


def test_the_page_shows_the_status_as_a_table(client, as_admin, db, clock):
    seed(db, clock, range(0, 15), head=1000, step=5)
    cursors(db, 900, 1100)

    page = client.get("/admin/scanner").text

    for label in (
        "Scanned up to lobby id",
        "Newest lobby id",
        "Lobby ids still to scan",
        "New lobby ids per minute",
        "Ids scanned per minute",
        "osu! requests per minute (budget 30)",
        "Open lobbies waiting to finish",
        "Last error",
    ):
        assert label in page
    assert "1,100" not in page and ">1100<" in page and ">900<" in page
    assert ">200<" in page
    assert "5.0 over 15 minutes" in page
    assert "about 40 minutes" in page
    assert "Running" in page
    assert 'hx-get="/admin/scanner/status"' in page and 'hx-trigger="every 15s"' in page


def test_the_status_block_alone_is_served_for_the_refresh(client, as_admin, db, clock):
    cursors(db, 900, 1100)

    block = client.get("/admin/scanner/status").text

    assert block.lstrip().startswith('<div id="scanner-status"')
    assert "<html" not in block and "Lobby scanner" not in block
    assert ">1100<" in block


def test_the_falling_behind_warning_is_shown_when_it_applies(client, as_admin, db, clock):
    seed(db, clock, range(0, 15), scanned=10, head=1000, step=15)
    cursors(db, 900, 1400)

    page = client.get("/admin/scanner").text

    assert (
        "The scanner is falling behind. Raise SCANNER_BUDGET_PER_MIN or expect skipped ranges."
        in page
    )


def test_no_warning_while_the_scanner_keeps_up(client, as_admin, db, clock):
    seed(db, clock, range(0, 15), scanned=10, head=1000, step=5)
    cursors(db, 1070, 1075)

    assert "falling behind" not in client.get("/admin/scanner").text


def test_pause_and_resume_flip_the_switch_and_the_button(client, as_admin, db):
    assert "Pause scanner" in client.get("/admin/scanner").text

    paused = client.post("/admin/scanner/pause", data=as_admin, follow_redirects=False)
    assert paused.status_code == 303
    assert Skimmer.is_paused(db) is True
    page = client.get("/admin/scanner").text
    assert "Resume scanner" in page and "Paused" in page

    client.post("/admin/scanner/resume", data=as_admin, follow_redirects=False)
    assert Skimmer.is_paused(db) is False
    assert "Pause scanner" in client.get("/admin/scanner").text


def test_a_switched_off_scanner_says_so_and_has_no_pause_button(client, app, as_admin, db):
    app.state.settings = dataclasses.replace(app.state.settings, scanner_enabled=False)

    page = client.get("/admin/scanner").text

    assert "Switched off" in page and "Pause scanner" not in page


def test_gaps_are_listed_with_a_backfill_button_that_starts_the_backfill(client, as_admin, db):
    gap = scan_repo.insert_gap(db, 1000, 1999, "skipped_backlog", "2026-11-12T12:00:00Z")

    page = client.get("/admin/scanner").text
    assert "1000 to 1999" in page and "1,000" in page and "Backfill" in page

    response = client.post(
        f"/admin/scanner/gaps/{gap}/backfill", data=as_admin, follow_redirects=False
    )

    assert response.status_code == 303
    assert scan_repo.get_gap(db, gap).backfilled_until == 999
    page = client.get("/admin/scanner").text
    assert "0 of 1,000 ids read" in page
    assert f'action="/admin/scanner/gaps/{gap}/backfill"' not in page  # nothing left to start
    assert f'action="/admin/scanner/gaps/{gap}/stop"' in page


def test_a_finished_backfill_is_marked_done(client, as_admin, db):
    gap = scan_repo.insert_gap(db, 10, 12, "skipped_backlog", "2026-11-12T12:00:00Z")
    scan_repo.request_backfill(db, gap)
    scan_repo.set_backfilled_until(db, gap, 12)

    assert "Done" in client.get("/admin/scanner").text


def test_a_gap_that_does_not_exist_is_a_404(client, as_admin):
    response = client.post("/admin/scanner/gaps/99/backfill", data=as_admin)

    assert response.status_code == 404


def test_without_gaps_the_page_says_so(client, as_admin):
    assert "The scanner has not skipped any ids." in client.get("/admin/scanner").text


def test_the_admin_page_links_to_the_scanner(client, as_admin):
    assert 'href="/admin/scanner"' in client.get("/admin").text


def test_starting_a_backfill_twice_says_it_is_already_running(client, as_admin, db):
    gap = scan_repo.insert_gap(db, 10, 20, "skipped_backlog", "2026-11-12T12:00:00Z")
    client.post(f"/admin/scanner/gaps/{gap}/backfill", data=as_admin)

    page = client.post(f"/admin/scanner/gaps/{gap}/backfill", data=as_admin).text  # redirected

    assert "This range is already being backfilled." in page
    assert scan_repo.get_gap(db, gap).backfilled_until == 9


def test_the_page_shows_the_errors_and_the_error_ranges_with_a_backfill_button(
    client, as_admin, db, clock
):
    scan_repo.add_stats(db, minute(clock, 2), ids_scanned=5, errors=2)
    gap = scan_repo.insert_gap(db, 70, 71, "fetch_error", "2026-11-12T12:00:00Z")

    page = client.get("/admin/scanner").text

    assert "Ids given up on after errors, last hour" in page
    assert re.search(r"Ids given up on after errors, last hour</th>\s*<td>2</td>", page)
    assert "70 to 71" in page and f"/admin/scanner/gaps/{gap}/backfill" in page


# --- starting from a lobby id ---

from app.services.skimmer import IdDispenser  # noqa: E402
from app.web.routes.scanner import _eta_text  # noqa: E402


def start(client, as_admin, value, **extra):
    return client.post(
        "/admin/scanner/start", data={**as_admin, "start": value, **extra}, follow_redirects=False
    )


def flashed(client):
    return client.get("/admin/scanner").text


def test_a_link_and_a_bare_id_give_the_same_range(client, as_admin, db):
    cursors(db, 1000, 1200)

    start(client, as_admin, "https://osu.ppy.sh/community/matches/900")
    first = [(g.start_id, g.end_id, g.reason) for g in scan_repo.list_gaps(db)]
    for gap in scan_repo.list_gaps(db):
        scan_repo.delete_gaps(db, [gap.id])
    start(client, as_admin, "900")

    assert first == [(900, 1000, "admin_skip")]
    assert [(g.start_id, g.end_id, g.reason) for g in scan_repo.list_gaps(db)] == first


def test_an_older_id_is_stored_as_a_backfill_and_the_page_says_so(client, as_admin, db):
    cursors(db, 1000, 1200)

    response = start(client, as_admin, "900")

    assert response.status_code == 303 and response.headers["location"] == "/admin/scanner"
    (gap,) = scan_repo.list_gaps(db)
    assert gap.backfilled_until == 899
    page = flashed(client)
    assert "Lobby ids 900 to 1,000 will be read in the background with spare budget" in page
    assert "Chosen by an admin" in page and "0 of 101 ids read" in page


def test_a_newer_id_makes_the_scanner_skip_ahead_and_lists_what_it_skipped(
    client, app, as_admin, db
):
    cursors(db, 1000, 1200)
    app.state.skimmer._dispenser = IdDispenser(1000, 1200)

    response = start(client, as_admin, "1100")

    assert response.status_code == 303
    (gap,) = scan_repo.list_gaps(db)
    assert (gap.start_id, gap.end_id, gap.backfilled_until) == (1001, 1099, None)
    assert jobs_repo.get(db, "skimmer").cursor == "1099"
    page = flashed(client)
    assert "The scanner now continues from lobby id 1,100." in page
    assert "Ids 1,001 to 1,099 are listed under Skipped ranges" in page


def test_the_id_the_scanner_already_continues_from_changes_nothing(client, as_admin, db):
    cursors(db, 1000, 1200)

    response = start(client, as_admin, "1001")

    assert response.status_code == 303 and scan_repo.list_gaps(db) == []
    assert "The scanner already continues from this lobby id." in flashed(client)


@pytest.mark.parametrize("value", ["abc", "", "https://example.com/matches/x", "-5", "12.5"])
def test_something_that_is_not_an_id_is_explained_next_to_the_field_and_kept(
    client, as_admin, db, value
):
    cursors(db, 1000, 1200)

    response = start(client, as_admin, value)

    assert response.status_code == 422
    assert "Enter a lobby id or an osu! match link." in response.text
    assert f'value="{value}"' in response.text
    assert scan_repo.list_gaps(db) == []


def test_an_id_above_the_head_is_refused_naming_the_head(client, as_admin, db):
    cursors(db, 1000, 1200)

    response = start(client, as_admin, "5000")

    assert response.status_code == 422
    assert "newer than the newest one osu! has listed (1,200)" in response.text
    assert 'value="5000"' in response.text


def test_before_the_scanner_has_read_the_head_the_admin_is_told_to_wait(client, as_admin, db):
    response = start(client, as_admin, "900")

    assert response.status_code == 422 and "has not started yet" in response.text


def test_a_skip_before_the_dispenser_exists_is_refused_and_stores_nothing(client, as_admin, db):
    cursors(db, 1000, 1200)  # the numbers are saved, but this process has not read the head yet

    response = start(client, as_admin, "1100")

    assert response.status_code == 422 and "has not started yet" in response.text
    assert scan_repo.list_gaps(db) == []


def test_a_switched_off_scanner_refuses_with_409(client, app, as_admin, db):
    cursors(db, 1000, 1200)
    app.state.settings = dataclasses.replace(app.state.settings, scanner_enabled=False)

    response = start(client, as_admin, "900")

    assert response.status_code == 409
    assert "The scanner is switched off (SCANNER_ENABLED is false)." in response.text
    assert scan_repo.list_gaps(db) == []


def test_the_start_form_is_not_offered_while_the_scanner_is_switched_off(client, app, as_admin):
    app.state.settings = dataclasses.replace(app.state.settings, scanner_enabled=False)

    assert "Start from a lobby id" not in client.get("/admin/scanner").text


def test_the_page_offers_the_start_form_with_the_scanners_position(client, as_admin, db):
    cursors(db, 1000, 1200)

    page = client.get("/admin/scanner").text

    assert "Start from a lobby id" in page and 'action="/admin/scanner/start"' in page
    assert "the scanner's position (1000)" in page and "newest id osu! has listed is 1200" in page


def test_a_large_backfill_asks_for_confirmation_before_anything_is_stored(
    client, app, as_admin, db
):
    cursors(db, 20_000, 20_100)
    limit = app.state.settings.scanner_max_backlog

    response = start(client, as_admin, "100")

    assert response.status_code == 200
    assert scan_repo.list_gaps(db) == []
    assert f"Read {20_000 - 100 + 1:,} lobby ids?" in response.text
    assert 'name="confirmed" value="1"' in response.text and 'value="100"' in response.text
    assert "Start backfill" in response.text and "(rough estimate)" in response.text
    assert 20_000 - 100 + 1 > limit


def test_confirming_stores_the_large_backfill_and_redirects(client, as_admin, db):
    cursors(db, 20_000, 20_100)

    response = start(client, as_admin, "100", confirmed="1")

    assert response.status_code == 303
    (gap,) = scan_repo.list_gaps(db)
    assert (gap.start_id, gap.end_id, gap.backfilled_until) == (100, 20_000, 99)


def test_a_backfill_of_exactly_the_limit_needs_no_confirmation(client, app, as_admin, db):
    limit = app.state.settings.scanner_max_backlog
    cursors(db, 10_000, 10_100)

    assert start(client, as_admin, str(10_000 - limit + 1)).status_code == 303
    assert start(client, as_admin, str(1)).status_code == 200  # and one bigger does


def test_the_confirmation_names_the_ranges_it_replaces_and_the_wait(client, as_admin, db, clock):
    cursors(db, 20_000, 20_100)
    scan_repo.insert_gap(db, 5000, 6000, "skipped_backlog", "2026-11-12T00:00:00Z")
    seed(db, clock, range(0, 60), scanned=1, requests=10)

    page = start(client, as_admin, "100").text

    assert "It replaces 1 skipped range it covers." in page
    assert "this takes about" in page


def test_with_no_spare_budget_the_confirmation_says_it_waits_for_the_live_scan(
    client, as_admin, db, clock
):
    cursors(db, 20_000, 20_100)
    seed(db, clock, range(0, 60), scanned=1, requests=30)

    page = start(client, as_admin, "100").text

    assert "There is no spare budget right now" in page


def test_the_ranges_table_says_why_and_offers_stop_for_a_running_backfill(client, as_admin, db):
    scan_repo.insert_gap(db, 1, 10, "skipped_backlog", "2026-11-12T00:00:00Z")
    scan_repo.insert_gap(db, 11, 20, "fetch_error", "2026-11-12T00:00:00Z")
    running = scan_repo.insert_gap(db, 21, 30, "admin_skip", "2026-11-12T00:00:00Z")
    scan_repo.request_backfill(db, running)

    page = client.get("/admin/scanner").text

    for text in ("Scanner fell behind", "osu! kept failing", "Chosen by an admin", "Why"):
        assert text in page
    assert page.count('action="/admin/scanner/gaps/') == 3
    assert f'action="/admin/scanner/gaps/{running}/stop"' in page


def test_stopping_a_backfill_keeps_what_was_read(client, as_admin, db):
    gap = scan_repo.insert_gap(db, 100, 199, "admin_skip", "2026-11-12T00:00:00Z")
    scan_repo.request_backfill(db, gap)
    scan_repo.set_backfilled_until(db, gap, 149)

    response = client.post(f"/admin/scanner/gaps/{gap}/stop", data=as_admin, follow_redirects=False)

    assert response.status_code == 303
    stopped = scan_repo.get_gap(db, gap)
    assert (stopped.start_id, stopped.backfilled_until) == (150, None)
    page = flashed(client)
    assert "Backfill stopped. What was read stays; the rest can be backfilled again." in page
    assert f'action="/admin/scanner/gaps/{gap}/backfill"' in page


def test_stopping_something_that_is_not_running_says_so(client, as_admin, db):
    gap = scan_repo.insert_gap(db, 100, 199, "skipped_backlog", "2026-11-12T00:00:00Z")

    response = client.post(f"/admin/scanner/gaps/{gap}/stop", data=as_admin, follow_redirects=False)

    assert response.status_code == 303
    assert "This range is not being backfilled." in flashed(client)


def test_stopping_an_unknown_range_is_a_404(client, as_admin):
    response = client.post("/admin/scanner/gaps/99/stop", data=as_admin)

    assert response.status_code == 404 and "This skipped range does not exist." in response.text


@pytest.mark.parametrize(
    ("minutes", "text"),
    [
        (None, "-"),
        (0.4, "less than a minute"),
        (30, "about 30 minutes"),
        (59.4, "about 59 minutes"),
        (180, "about 3 hours"),
        (60 * 24, "about 1 days"),
        (60 * 24 * 2.4, "about 2 days"),
    ],
)
def test_the_wait_is_said_in_minutes_hours_or_days(minutes, text):
    assert _eta_text(minutes) == text


def live_ahead(app, db):
    """The saved cursor lags: 1000 on disk, 1100 in the running scanner."""
    cursors(db, 1000, 1200)
    app.state.skimmer._dispenser = IdDispenser(1100, 1200)


def test_the_start_id_the_live_scanner_already_continues_from_changes_nothing(
    client, app, as_admin, db
):
    live_ahead(app, db)

    response = start(client, as_admin, "1101")

    assert response.status_code == 303 and scan_repo.list_gaps(db) == []
    assert "The scanner already continues from this lobby id." in flashed(client)
    assert jobs_repo.get(db, "skimmer").cursor == "1000"


def test_a_backfill_is_planned_from_the_live_cursor_not_the_saved_one(client, app, as_admin, db):
    live_ahead(app, db)

    start(client, as_admin, "1050")

    (gap,) = scan_repo.list_gaps(db)
    assert (gap.start_id, gap.end_id) == (1050, 1100)  # not 1050 to 1000, nor a refusal


def test_a_skip_is_planned_from_the_live_cursor(client, app, as_admin, db):
    live_ahead(app, db)

    response = start(client, as_admin, "1150")

    assert response.status_code == 303
    (gap,) = scan_repo.list_gaps(db)
    assert (gap.start_id, gap.end_id) == (1101, 1149)  # the ids up to 1100 were already read
    assert app.state.skimmer.live_cursor == 1149
