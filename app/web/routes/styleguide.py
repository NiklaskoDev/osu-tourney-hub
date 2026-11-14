from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.domain.slots import SLOT
from app.services import availability
from app.services.availability import Column, MatchBand
from app.web.deps import require_admin
from app.web.templating import render

router = APIRouter()

SAMPLE_MONDAY = date(2026, 11, 16)


def _slots(first_hour: float, last_hour: float) -> list[str]:
    """Slot starts (UTC text) from `first_hour` up to `last_hour` on the sample Monday."""
    start = datetime(2026, 11, 16, tzinfo=UTC) + timedelta(hours=first_hour)
    count = int((last_hour - first_hour) * 2)
    return [(start + SLOT * i).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(count)]


def _sample_day() -> availability.DayTimeline:
    """One day built by the real timeline builder from made-up answers: yes and maybe runs, a
    player who has not filled in, and a match band."""
    columns = [Column(i, f"Player {name}") for i, name in enumerate("ABCD", start=1)]
    found = {
        1: {**dict.fromkeys(_slots(18, 23), "yes"), **dict.fromkeys(_slots(23, 24), "maybe")},
        2: dict.fromkeys(_slots(17, 21), "yes"),
        3: {**dict.fromkeys(_slots(19, 22), "yes"), **dict.fromkeys(_slots(22, 23), "maybe")},
    }
    bands = [MatchBand(datetime(2026, 11, 16, 19, 30, tzinfo=UTC), "vs Team B (sample)")]
    return availability.build_timelines(SAMPLE_MONDAY, UTC, columns, found, bands, {1, 2, 3})[0]


def _sample_upcoming() -> list[SimpleNamespace]:
    """Three upcoming matches with sample team names, shaped like schedule.UpcomingMatch."""
    rows = [
        ("Team B (sample)", "Round of 16", "Match 3"),
        ("Team C (sample)", "Quarterfinals", None),
    ]
    rows.append(("Team D (sample)", "Semifinals", "Match 1"))
    return [
        SimpleNamespace(
            tournament=SimpleNamespace(id=0, acronym="SC26", name="Spring Cup"),
            view=SimpleNamespace(
                match=SimpleNamespace(
                    id=index, label=label, scheduled_at=f"2030-01-0{index + 1}T20:00:00Z"
                ),
                opponent_name=opponent,
                stage_name=stage,
            ),
        )
        for index, (opponent, stage, label) in enumerate(rows)
    ]


@router.get("/styleguide", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def styleguide(request: Request) -> HTMLResponse:
    """Every component in every state, for design review. Administrators only."""
    context = {"sample_day": _sample_day(), "sample_upcoming": _sample_upcoming()}
    return render(request, "pages/styleguide/index.html", context)
