"""Which stage a page opens on: the one named, else the current round of an active tournament."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.models import Stage
from app.services import pool_sheet
from tests.support import add_member, add_pool_map, add_score, add_stage, add_tournament

PLAYER = 2002


def at(day: float) -> datetime:
    """A moment `day` days into November 2026 (1.0 is midnight on the 1st)."""
    return datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=day - 1)


def stage(stage_id: int, start: float | None, end: float | None, released: bool = True) -> Stage:
    def stamp(day: float | None) -> str | None:
        return None if day is None else at(day).strftime("%Y-%m-%dT%H:%M:%SZ")

    return Stage(
        id=stage_id,
        tournament_id=1,
        wybin_stage_id=None,
        name=f"Stage {stage_id}",
        sort_order=stage_id,
        start_at=stamp(start),
        end_at=stamp(end),
        best_of=None,
        bans=None,
        protects=None,
        is_qualifier=False,
        lineup_size=None,
        pool_released=released,
    )


A, B, C = stage(1, 1, 7), stage(2, 8, 14), stage(3, 15, 21)
STAGES = [A, B, C]


# --- current_stage ------------------------------------------------------------------------------


def test_the_running_stage_is_the_current_one():
    assert pool_sheet.current_stage(STAGES, at(10)) is B


def test_between_two_stages_the_next_one_to_start_is_current():
    assert pool_sheet.current_stage(STAGES, at(7.5)) is B


def test_after_the_last_stage_the_last_one_is_current():
    assert pool_sheet.current_stage(STAGES, at(30)) is C


def test_before_every_stage_the_first_to_start_is_current():
    assert pool_sheet.current_stage(STAGES, at(0.5)) is A


def test_a_stage_ends_at_its_end_time_and_starts_at_its_start_time():
    assert pool_sheet.current_stage(STAGES, at(8)) is B
    assert pool_sheet.current_stage([A, stage(2, 7, 14)], at(7)).id == 2


def test_without_any_date_the_first_released_pool_wins_as_before():
    undated = [stage(1, None, None, released=False), stage(2, None, None), stage(3, None, None)]

    assert pool_sheet.current_stage(undated, at(10)).id == 2


def test_without_any_date_and_no_released_pool_the_first_stage_wins():
    undated = [stage(1, None, None, released=False), stage(2, None, None, released=False)]

    assert pool_sheet.current_stage(undated, at(10)).id == 1


def test_of_two_running_stages_the_later_start_wins():
    wide, narrow = stage(1, 1, 30), stage(2, 10, 20)

    assert pool_sheet.current_stage([wide, narrow], at(12)) is narrow


def test_a_running_stage_without_a_start_counts_as_the_earliest():
    open_start, dated = stage(1, None, 30), stage(2, 5, 20)

    assert pool_sheet.current_stage([open_start, dated], at(12)) is dated
    assert pool_sheet.current_stage([open_start, dated], at(25)) is open_start


def test_a_stage_with_only_an_end_that_has_not_passed_is_running():
    only_end = stage(1, None, 10)

    assert pool_sheet.current_stage([only_end, stage(2, 20, 30)], at(5)) is only_end
    assert pool_sheet.current_stage([only_end, stage(2, 20, 30)], at(12)).id == 2


def test_no_stages_means_no_current_stage():
    assert pool_sheet.current_stage([], at(1)) is None


# --- pick_stage ---------------------------------------------------------------------------------


def test_a_named_stage_beats_the_current_round():
    assert pool_sheet.pick_stage(STAGES, "1", at(10)) is A


def test_an_unknown_name_falls_back_to_the_current_round_when_now_is_given():
    assert pool_sheet.pick_stage(STAGES, "99", at(10)) is B
    assert pool_sheet.pick_stage(STAGES, None, at(10)) is B


def test_without_now_the_first_released_pool_wins_as_before():
    stages = [stage(1, 1, 7, released=False), stage(2, 8, 14), stage(3, 15, 21)]

    assert pool_sheet.pick_stage(stages, None).id == 2
    assert pool_sheet.pick_stage(stages, "").id == 2
    assert pool_sheet.pick_stage([], None) is None


# --- on the pages -------------------------------------------------------------------------------


@pytest.fixture
def dated_cup(db):
    """An active tournament whose stages run: Qualifiers last week, Groups now, Finals next week."""
    tid = add_tournament(db, "Dated Cup")
    add_member(db, tid, PLAYER, "captain")
    ids = {}
    for order, (name, start, end) in enumerate(
        [
            ("Qualifiers", "2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z"),
            ("Groups", "2026-11-09T00:00:00Z", "2026-11-16T00:00:00Z"),
            ("Finals", "2026-11-17T00:00:00Z", "2026-11-24T00:00:00Z"),
        ],
        start=1,
    ):
        ids[name] = add_stage(db, tid, name, order)
        db.execute(
            "UPDATE stages SET start_at = ?, end_at = ? WHERE id = ?", (start, end, ids[name])
        )
        add_pool_map(db, ids[name], "NM1", beatmap_id=5000 + order)
    return {"id": tid, **ids}


def current_tab(html: str) -> str:
    start = html.index('class="tabs__tab" href="')
    marker = 'aria-current="page">'
    return html[html.index(marker, start) + len(marker) :].split("<")[0]


@pytest.mark.parametrize("path", ["pool", "lineup", "coverage", "pickems"])
def test_stage_pages_of_an_active_tournament_open_on_the_current_round(
    client, login, dated_cup, path
):
    login(PLAYER)

    html = client.get(f"/t/{dated_cup['id']}/{path}").text

    assert current_tab(html) == "Groups"


@pytest.mark.parametrize("path", ["pool", "lineup", "coverage", "pickems"])
def test_a_named_stage_still_wins_on_the_stage_pages(client, login, dated_cup, path):
    login(PLAYER)

    html = client.get(f"/t/{dated_cup['id']}/{path}", params={"stage": dated_cup["Finals"]}).text

    assert current_tab(html) == "Finals"


def test_an_archived_tournament_keeps_the_old_rule(client, login, db, dated_cup):
    db.execute("UPDATE tournaments SET status = 'archived' WHERE id = ?", (dated_cup["id"],))
    login(PLAYER)

    html = client.get(f"/t/{dated_cup['id']}/pool").text

    assert current_tab(html) == "Qualifiers"  # the first stage with a released pool


def test_the_import_page_preselects_the_current_round(client, login, dated_cup):
    login(PLAYER)

    html = client.get(f"/t/{dated_cup['id']}/import").text

    assert f'name="stage" value="{dated_cup["Groups"]}"' in html


def test_the_csv_export_without_a_stage_uses_the_current_round(client, login, db, dated_cup):
    add_score(db, 5002, PLAYER, 800_000)  # on the Groups map
    login(PLAYER)

    response = client.get(f"/t/{dated_cup['id']}/export/pool.csv")

    assert response.status_code == 200
    assert "NM1,5002," in response.text


def test_lobbies_open_on_the_current_round_and_an_empty_stage_means_all(client, login, dated_cup):
    login(PLAYER)

    default = client.get(f"/t/{dated_cup['id']}/lobbies").text
    everything = client.get(f"/t/{dated_cup['id']}/lobbies", params={"stage": ""}).text

    assert f'<option value="{dated_cup["Groups"]}" selected>' in default
    assert 'value="" selected' in everything
    assert "selected>Groups" not in everything
