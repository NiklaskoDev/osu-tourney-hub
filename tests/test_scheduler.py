import asyncio
import time
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.clients.wybin_models import WybinUnavailable
from app.db.connection import open_db
from app.main import create_app
from app.repos import jobs as jobs_repo
from app.workers.jobs import SYNC_ACTIVE, TRACK_SIGNUPS, build_jobs
from app.workers.scheduler import GENERIC_ERROR_TEXT, WYBIN_ERROR_TEXT, Job, Scheduler
from tests.conftest import BASE_URL
from tests.support import NoRealNetwork, add_test_login


@pytest.fixture
def scheduler_for(client, settings, clock):
    def make(*jobs: Job, sleep=None) -> Scheduler:
        return Scheduler(settings.db_path, clock, list(jobs), **({"sleep": sleep} if sleep else {}))

    return make


def state(db, name):
    return jobs_repo.get(db, name)


def run(coroutine):
    return asyncio.run(coroutine)


def job(name, action, hours=1) -> Job:
    return Job(name, timedelta(hours=hours), action)


# --- recording outcomes ---


def test_a_successful_run_records_when_it_ran_and_succeeded(db, scheduler_for):
    async def ok() -> None:
        return None

    assert run(scheduler_for().run_once(job("demo", ok))) is True

    row = state(db, "demo")
    assert row.last_run_at == "2026-11-12T12:30:15Z"
    assert row.last_ok_at == "2026-11-12T12:30:15Z"
    assert row.last_error is None


def test_a_wybin_failure_is_recorded_as_generic_text_without_the_details(db, scheduler_for):
    async def down() -> None:
        raise WybinUnavailable("https://wybin.xyz/api/v1/secret-path?token=abc")

    assert run(scheduler_for().run_once(job("demo", down))) is False

    row = state(db, "demo")
    assert row.last_error == WYBIN_ERROR_TEXT
    assert "secret-path" not in row.last_error and "token" not in row.last_error
    assert row.last_ok_at is None
    assert row.last_run_at is not None


def test_an_unexpected_error_is_recorded_generically_and_logged(db, scheduler_for, caplog):
    async def broken() -> None:
        raise RuntimeError("database password is hunter2")

    assert run(scheduler_for().run_once(job("demo", broken))) is False

    assert state(db, "demo").last_error == GENERIC_ERROR_TEXT
    assert "hunter2" not in state(db, "demo").last_error
    assert "demo failed" in caplog.text  # the details go to the server log


def test_a_later_success_clears_the_error_and_keeps_the_last_ok_time(db, scheduler_for, clock):
    outcomes = [RuntimeError("x"), None]

    async def flaky() -> None:
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    scheduler = scheduler_for()
    demo = job("demo", flaky)
    run(scheduler.run_once(demo))
    assert state(db, "demo").last_error == GENERIC_ERROR_TEXT
    assert state(db, "demo").last_ok_at is None

    clock.current += timedelta(hours=1)
    run(scheduler.run_once(demo))

    row = state(db, "demo")
    assert row.last_error is None
    assert row.last_ok_at == "2026-11-12T13:30:15Z"


def test_a_failure_after_a_success_keeps_the_earlier_ok_time(db, scheduler_for, clock):
    outcomes = [None, RuntimeError("x")]

    async def flaky() -> None:
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    scheduler = scheduler_for()
    demo = job("demo", flaky)
    run(scheduler.run_once(demo))
    clock.current += timedelta(hours=1)
    run(scheduler.run_once(demo))

    row = state(db, "demo")
    assert row.last_ok_at == "2026-11-12T12:30:15Z"
    assert row.last_run_at == "2026-11-12T13:30:15Z"
    assert row.last_error == GENERIC_ERROR_TEXT


# --- the loop ---


def test_jobs_run_at_once_then_every_interval_and_one_failing_job_never_stops_the_others(
    db, scheduler_for
):
    calls = {"failing": 0, "healthy": 0}
    sleeps: list[float] = []

    async def failing() -> None:
        calls["failing"] += 1
        raise RuntimeError("always")

    async def healthy() -> None:
        calls["healthy"] += 1

    async def instant_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    async def scenario() -> None:
        scheduler = scheduler_for(
            job("failing", failing, hours=1), job("healthy", healthy, hours=6), sleep=instant_sleep
        )
        scheduler.start()
        for _ in range(30):
            await asyncio.sleep(0)
        await scheduler.stop()

    run(scenario())

    assert calls["failing"] >= 3 and calls["healthy"] >= 3
    assert set(sleeps) == {3600.0, 21600.0}
    assert state(db, "healthy").last_ok_at is not None
    assert state(db, "failing").last_error == GENERIC_ERROR_TEXT


def test_stop_cancels_waiting_jobs(scheduler_for):
    async def idle() -> None:
        return None

    async def scenario() -> list[bool]:
        scheduler = scheduler_for(job("demo", idle))  # real sleep: an hour
        scheduler.start()
        await asyncio.sleep(0.05)
        tasks = list(scheduler._tasks)
        await scheduler.stop()
        return [task.done() for task in tasks]

    assert run(scenario()) == [True]


def test_a_job_can_be_looked_up_by_name(scheduler_for):
    async def idle() -> None:
        return None

    scheduler = scheduler_for(job("a", idle), job("b", idle))

    assert scheduler.job("b").name == "b"


# --- the app's own jobs ---


def test_the_app_registers_the_wybin_jobs_with_the_configured_interval(app, settings, clock):
    jobs = {item.name: item for item in build_jobs(settings, clock, app.state.wybin)}

    assert set(jobs) == {TRACK_SIGNUPS, SYNC_ACTIVE}
    assert jobs[TRACK_SIGNUPS].interval == timedelta(hours=6)
    assert jobs[SYNC_ACTIVE].interval == timedelta(minutes=30)


def test_workers_start_with_the_app_and_stop_with_it(settings, clock, osu, wybin):
    wybin.load_defaults()
    network = NoRealNetwork(osu, wybin)
    app = create_app(settings, clock, http_transport=network.transport, start_workers=True)
    add_test_login(app)

    with TestClient(app, base_url=BASE_URL):
        deadline = time.monotonic() + 5
        seen: set[str] = set()
        while time.monotonic() < deadline and seen != {TRACK_SIGNUPS, SYNC_ACTIVE}:
            with closing(open_db(settings.db_path)) as conn:
                seen = {
                    name
                    for name in (TRACK_SIGNUPS, SYNC_ACTIVE)
                    if (row := jobs_repo.get(conn, name)) and row.last_ok_at
                }
            time.sleep(0.02)
        assert seen == {TRACK_SIGNUPS, SYNC_ACTIVE}

    assert all(task.done() for task in app.state.scheduler._tasks) or not app.state.scheduler._tasks


def test_without_workers_nothing_runs(client, settings):
    with closing(open_db(settings.db_path)) as conn:
        assert jobs_repo.get(conn, TRACK_SIGNUPS) is None


# --- what the admin page can show about the wybin jobs ---


def test_a_partial_failure_is_recorded_with_its_counts_and_no_false_promise(
    client, db, settings, clock, app, wybin
):
    wybin.load_defaults()
    from tests.support import add_user

    add_user(db, 1001, "AdminAmy")
    sync_job = {item.name: item for item in build_jobs(settings, clock, app.state.wybin)}[
        SYNC_ACTIVE
    ]
    scheduler = Scheduler(settings.db_path, clock, [sync_job])
    from app.services.wybin_sync import import_tournament

    run(import_tournament(db, app.state.wybin, "synthetic-cup", 1001, clock))
    run(import_tournament(db, app.state.wybin, "synthetic-solo", 1001, clock))
    wybin.mappools.clear()  # the pool endpoint now fails for both
    del wybin.tournaments["synthetic-solo"]  # and wybin lost the solo tournament

    assert run(scheduler.run_once(sync_job)) is False

    row = state(db, SYNC_ACTIVE)
    assert row.last_error == (
        "wybin could not be read for 1 of 2 tournaments; the map pool could not be updated for 1. "
        "Nothing was changed for what failed."
    )
    assert row.last_ok_at is None


def test_the_generic_wybin_message_does_not_promise_unchanged_data():
    assert "left unchanged" not in WYBIN_ERROR_TEXT
    assert WYBIN_ERROR_TEXT.endswith("Nothing was changed for what failed.")
