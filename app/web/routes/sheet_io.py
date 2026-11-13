"""CSV export of the pool sheet and lineup, and the score import (file or Google Sheets link) with
its preview step. Everyone on the roster may export; captains and above import."""

import re
import sqlite3
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from app.clock import Clock
from app.domain.models import Stage, Tournament
from app.domain.parsing import parse_uint
from app.repos import stages as stages_repo
from app.services import csv_io, pool_sheet
from app.services.csv_text import (
    MAX_CSV_BYTES,
    CsvImportError,
    CsvTable,
    decode_upload,
    parse_table,
)
from app.services.sheets_link import fetch_sheet_csv
from app.services.tournament_settings import SCORING_LABELS
from app.web.deps import (
    TournamentAccess,
    captain_access,
    get_clock,
    get_db,
    get_http,
    member_access,
    require_writable,
)
from app.web.errors import UserFacingError
from app.web.security import SlidingWindowLimiter
from app.web.session import flash
from app.web.templating import render

router = APIRouter(prefix="/t/{tid}")

NO_INPUT = "Choose a CSV file or paste the link of a Google Sheet."
NO_STAGE = "This tournament has no stages yet, so there is nothing to import into."
NOTHING_TO_IMPORT = "No row can be imported yet. Fix the columns or the rows listed below."
SHOWN_PROBLEMS = 50
SHOWN_SAMPLE = 10
SHOWN_UNKNOWN = 30


def _filename(tournament: Tournament, stage: Stage | None, kind: str) -> str:
    name = (
        f"{tournament.acronym or tournament.name}-{stage.name}"
        if stage
        else (tournament.acronym or tournament.name)
    )
    base = re.sub(r"[^A-Za-z0-9]+", "-", name)
    return f"{base.strip('-') or 'tournament'}-{kind}.csv"


def _csv_response(text: str, filename: str) -> Response:
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _stage_for_export(db: sqlite3.Connection, tournament: Tournament, raw: str | None) -> Stage:
    stage = pool_sheet.pick_stage(stages_repo.list_for_tournament(db, tournament.id), raw)
    if stage is None:
        raise UserFacingError(404, "This tournament has no stages to export.")
    return stage


@router.get("/export/pool.csv")
async def export_pool(
    stage: str | None = None,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    chosen = _stage_for_export(db, access.tournament, stage)
    return _csv_response(
        csv_io.export_pool(db, access.tournament, chosen),
        _filename(access.tournament, chosen, "pool"),
    )


@router.get("/export/lineup.csv")
async def export_lineup(
    stage: str | None = None,
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    chosen = _stage_for_export(db, access.tournament, stage)
    return _csv_response(
        csv_io.export_lineup(db, access.tournament, chosen),
        _filename(access.tournament, chosen, "lineup"),
    )


@router.get("/export/availability.csv")
async def export_availability(
    access: TournamentAccess = Depends(member_access),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    return _csv_response(
        csv_io.export_availability(db, access.tournament),
        _filename(access.tournament, None, "availability"),
    )


# --- import ----------------------------------------------------------------------------------


def _page(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    stage_ref: str | None,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    tournament = access.tournament
    stages = stages_repo.list_for_tournament(db, tournament.id)
    stage = pool_sheet.pick_stage(stages, stage_ref)
    context: dict[str, Any] = {
        "access": access,
        "tournament": tournament,
        "nav_current": "pool",
        "archived": tournament.status == "archived",
        "stage": stage,
        "scoring_label": SCORING_LABELS[tournament.scoring],
        "stage_options": [(str(s.id), s.name) for s in stages],
        **extra,
    }
    return render(request, "pages/tournament/import.html", context, status_code=status_code)


def _preview(
    request: Request,
    access: TournamentAccess,
    db: sqlite3.Connection,
    text: str,
    stage_ref: str | None,
    form_values: dict[str, str] | None = None,
    status_code: int = 200,
    error: str | None = None,
) -> HTMLResponse:
    """The preview page: the columns as detected (or as chosen), then every row checked."""
    try:
        table = parse_table(text)
    except CsvImportError as refused:
        return _page(request, access, db, stage_ref, 422, upload_error=str(refused))
    stages = stages_repo.list_for_tournament(db, access.tournament.id)
    stage = pool_sheet.pick_stage(stages, stage_ref)
    if stage is None:
        return _page(request, access, db, None, 422, upload_error=NO_STAGE)
    if form_values is None:
        mapping = csv_io.detect_mapping(table.headers)
    else:
        mapping = csv_io.mapping_from_form(form_values, len(table.headers))
    problems = csv_io.mapping_problems(mapping)
    plan = None if problems else csv_io.plan_import(db, access.tournament, stage, table, mapping)
    return _page(
        request,
        access,
        db,
        str(stage.id),
        status_code,
        **_preview_context(text, table, mapping, problems, plan),
        import_error=error,
    )


def _preview_context(
    text: str,
    table: CsvTable,
    mapping: csv_io.Mapping,
    mapping_problems: list[str],
    plan: csv_io.ImportPlan | None,
) -> dict[str, Any]:
    columns = [("", "Not in the file")] + [
        (str(index), header or f"Column {index + 1}") for index, header in enumerate(table.headers)
    ]
    return {
        "csv_text": text,
        "column_options": columns,
        "mapping_fields": [
            (name, label, "" if mapping.get(name) is None else str(mapping[name]))
            for name, label in csv_io.FIELD_LABELS.items()
        ],
        "mapping_problems": mapping_problems,
        "plan": plan,
        "shown_problems": plan.problems[:SHOWN_PROBLEMS] if plan else [],
        "more_problems": max(0, len(plan.problems) - SHOWN_PROBLEMS) if plan else 0,
        "sample": plan.scores[:SHOWN_SAMPLE] if plan else [],
        "unknown_players": plan.unknown_players[:SHOWN_UNKNOWN] if plan else [],
        "more_unknown": max(0, len(plan.unknown_players) - SHOWN_UNKNOWN) if plan else 0,
    }


def _limit_imports(request: Request, osu_id: int) -> None:
    limiter: SlidingWindowLimiter = request.app.state.import_limiter
    if not limiter.allow(str(osu_id)):
        raise UserFacingError(
            429,
            "You have started many imports. Try again later.",
            headers={"Retry-After": str(limiter.retry_after_seconds(str(osu_id)))},
        )


async def _form_values(request: Request) -> dict[str, str]:
    fields = await request.form()
    return {key: value for key, value in fields.items() if isinstance(value, str)}


@router.get("/import", response_class=HTMLResponse)
async def import_page(
    request: Request,
    stage: str | None = None,
    access: TournamentAccess = Depends(captain_access),
    db: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return _page(request, access, db, stage)


@router.post("/import")
async def start_import(
    request: Request,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    http: httpx.AsyncClient = Depends(get_http),
) -> Response:
    """Read the uploaded file or fetch the Google Sheet, then show the preview."""
    _limit_imports(request, access.user.osu_id)
    fields = await request.form()
    stage_ref = fields.get("stage")
    stage_ref = stage_ref if isinstance(stage_ref, str) else None
    upload = fields.get("file")
    link = fields.get("link")
    try:
        if isinstance(upload, UploadFile) and upload.filename:
            text = decode_upload(await upload.read(MAX_CSV_BYTES + 1))
        elif isinstance(link, str) and link.strip():
            text = await fetch_sheet_csv(http, link)
        else:
            raise CsvImportError(NO_INPUT)
    except CsvImportError as refused:
        return _page(
            request,
            access,
            db,
            stage_ref,
            422,
            upload_error=str(refused),
            link_value=link if isinstance(link, str) else "",
        )
    return _preview(request, access, db, text, stage_ref)


@router.post("/import/preview")
async def update_preview(
    request: Request,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Check the same file again with the columns and stage the person chose."""
    values = await _form_values(request)
    return _preview(
        request, access, db, values.get("csv_text", ""), values.get("stage"), form_values=values
    )


@router.post("/import/commit")
async def commit_import(
    request: Request,
    access: TournamentAccess = Depends(captain_access),
    _writable: TournamentAccess = Depends(require_writable),
    db: sqlite3.Connection = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Check everything once more and store the scores that are fine and new."""
    values = await _form_values(request)
    text = values.get("csv_text", "")
    try:
        table = parse_table(text)
    except CsvImportError as refused:
        return _page(request, access, db, values.get("stage"), 422, upload_error=str(refused))
    stage_id = parse_uint(values.get("stage", ""))
    stage = stages_repo.get(db, stage_id) if stage_id is not None else None
    if stage is None or stage.tournament_id != access.tournament.id:
        return _page(request, access, db, None, 422, upload_error=NO_STAGE)
    mapping = csv_io.mapping_from_form(values, len(table.headers))
    plan = None
    if not csv_io.mapping_problems(mapping):
        plan = csv_io.plan_import(db, access.tournament, stage, table, mapping)
    if plan is None or not plan.scores:
        return _preview(
            request,
            access,
            db,
            text,
            str(stage.id),
            form_values=values,
            status_code=422,
            error=NOTHING_TO_IMPORT,
        )
    imported = csv_io.commit_import(db, access.tournament, plan, access.user.osu_id, clock.now())
    skipped = plan.duplicates
    flash(
        request,
        "success",
        f"Imported {imported} {'score' if imported == 1 else 'scores'}."
        + (f" {skipped} already on the sheet, skipped." if skipped else "")
        + (f" {len(plan.problems)} rows were left out." if plan.problems else ""),
    )
    return RedirectResponse(f"/t/{access.tournament.id}/pool?stage={stage.id}", status_code=303)
