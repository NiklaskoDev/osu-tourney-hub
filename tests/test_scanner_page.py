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
    assert 'action="/admin/scanner/gaps/' not in page  # nothing left to start


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
