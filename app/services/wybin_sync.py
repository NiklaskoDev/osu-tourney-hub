"""Import tournaments from wybin and keep them up to date.

Every sync fetches first and writes afterwards in one transaction: a failed request leaves the
stored data exactly as it was. Syncs only ever add or refresh; they never remove a member, and
they never touch the app's own settings (freemod rule, scoring, lineup sizes, status, placing).
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.clients.wybin import WybinClient, parse_slug
from app.clients.wybin_models import (
    WybinBeatmap,
    WybinBracket,
    WybinError,
    WybinFormatError,
    WybinMatch,
    WybinNotFound,
    WybinPlayer,
    WybinStage,
    WybinTournament,
)
from app.clock import Clock, to_utc_iso
from app.db.connection import transaction
from app.domain.models import OsuProfile
from app.domain.modes import (
    FREEMOD_SLOT,
    NOMOD_SLOT,
    TIEBREAKER_SLOT,
    is_freemod_bracket,
    is_tiebreaker_bracket,
    mods_from_bitmask,
    required_mods_for_prefix,
    slot_prefix,
)
from app.repos import deleted_tournaments as deleted_repo
from app.repos import members as members_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos import tournaments as tournaments_repo
from app.repos import users as users_repo
from app.repos import wybin_links as links_repo
from app.repos.teams import PlayerValues
from app.services.match_sync import apply_matches

logger = logging.getLogger(__name__)

# A tournament found by sign-up tracking whose last stage ended this long ago is history: it is
# imported as archived so it is not polled forever.
STALE_AFTER = timedelta(days=30)


class WybinSyncError(WybinError):
    """One or more wybin requests of a background run failed; the message is safe to show."""


class TournamentArchivedError(Exception):
    """The tournament is archived, so a sync or import must not rewrite it."""


@dataclass(frozen=True)
class SyncResult:
    tournament_id: int
    created: bool
    # True when the mappool endpoint could not be read, so pools were not refreshed.
    pools_unreadable: bool = False
    # Likewise for the matches endpoint.
    matches_unreadable: bool = False


@dataclass(frozen=True)
class TrackResult:
    imported: int


async def import_tournament(
    conn: sqlite3.Connection,
    wybin: WybinClient,
    link_or_slug: str,
    importer_osu_id: int,
    clock: Clock,
    archive_if_ended: bool = False,
) -> SyncResult:
    """Import (or refresh) a tournament by wybin address or slug.

    Raises `InvalidWybinLink` for text that is not a wybin tournament, `WybinError` subclasses
    when wybin cannot be read, and `TournamentArchivedError` when the tournament is already here
    and archived (it stays as it is until an admin restores it).
    """
    slug = parse_slug(link_or_slug)
    known = tournaments_repo.find_by_slug(conn, slug)
    if known is not None and known.status == "archived":
        raise TournamentArchivedError(slug)
    data = await wybin.get_tournament(slug)
    pools, readable = await _fetch_pools(wybin, data)
    matches = await _fetch_matches(wybin, data)
    return _apply(
        conn, data, pools, matches, importer_osu_id, clock.now(), archive_if_ended, readable
    )


async def sync_tournament(
    conn: sqlite3.Connection, wybin: WybinClient, tournament_id: int, clock: Clock
) -> SyncResult:
    """Refresh a wybin tournament. Raises `LookupError` when it is not one."""
    tournament = tournaments_repo.get(conn, tournament_id)
    if tournament is None or tournament.source != "wybin" or not tournament.slug:
        raise LookupError("not a wybin tournament")
    data = await wybin.get_tournament(tournament.slug)
    pools, readable = await _fetch_pools(wybin, data)
    matches = await _fetch_matches(wybin, data)
    return _apply(conn, data, pools, matches, tournament.owner_osu_id, clock.now(), False, readable)


async def sync_active(conn: sqlite3.Connection, wybin: WybinClient, clock: Clock) -> int:
    """Sync every active wybin tournament. One failing tournament does not stop the others.

    Tournaments wybin could not be read for, and tournaments whose pool could not be refreshed
    (the rest of their data was), are counted and reported at the end as `WybinSyncError`, so the
    job state shows the problem."""
    failed = pools_unreadable = matches_unreadable = 0
    tournaments = tournaments_repo.list_syncable(conn)
    for tournament in tournaments:
        try:
            result = await sync_tournament(conn, wybin, tournament.id, clock)
        except WybinError as error:
            logger.warning(
                "wybin sync of tournament %s failed: %s", tournament.id, type(error).__name__
            )
            failed += 1
        else:
            pools_unreadable += result.pools_unreadable
            matches_unreadable += result.matches_unreadable
    problems = []
    if failed:
        problems.append(f"wybin could not be read for {failed} of {len(tournaments)} tournaments")
    if pools_unreadable:
        problems.append(f"the map pool could not be updated for {pools_unreadable}")
    if matches_unreadable:
        problems.append(f"the matches could not be updated for {matches_unreadable}")
    if problems:
        raise WybinSyncError("; ".join(problems) + ". Nothing was changed for what failed.")
    return len(tournaments)


async def track_signups(conn: sqlite3.Connection, wybin: WybinClient, clock: Clock) -> TrackResult:
    """Import tournaments the linked admins signed up for on wybin.

    Anything already in the database counts as known, hidden ones included, so a tournament an
    admin hid is never brought back; the same goes for tournaments an admin deleted.
    """
    imported = failed = pools_unreadable = matches_unreadable = 0
    for link in links_repo.list_all(conn):
        try:
            detail = await wybin.get_user_detailed(link.wybin_user_id)
        except WybinError as error:
            logger.warning("wybin sign-up check for admin %s failed: %s", link.osu_id, error)
            failed += 1
            continue
        for ref in detail.tournaments:
            if _is_known(conn, ref.wybin_id, ref.slug) or not ref.slug:
                continue
            try:
                result = await import_tournament(
                    conn, wybin, ref.slug, link.osu_id, clock, archive_if_ended=True
                )
            except (WybinError, ValueError, TournamentArchivedError) as error:
                logger.warning("wybin import for admin %s failed: %s", link.osu_id, error)
                failed += 1
            else:
                imported += 1
                pools_unreadable += result.pools_unreadable
                matches_unreadable += result.matches_unreadable
        links_repo.mark_checked(conn, link.osu_id, to_utc_iso(clock.now()))
    problems = []
    if failed:
        problems.append(f"{failed} wybin requests failed while checking sign-ups")
    if pools_unreadable:
        problems.append(f"the map pool could not be read for {pools_unreadable} imported")
    if matches_unreadable:
        problems.append(f"the matches could not be read for {matches_unreadable} imported")
    if problems:
        raise WybinSyncError("; ".join(problems) + ".")
    return TrackResult(imported)


def _is_known(conn: sqlite3.Connection, wybin_id: int | None, slug: str | None) -> bool:
    if deleted_repo.is_deleted(conn, wybin_id, slug):
        return True
    if wybin_id is not None and tournaments_repo.find_by_wybin_id(conn, wybin_id):
        return True
    return slug is not None and tournaments_repo.find_by_slug(conn, slug) is not None


async def _fetch_pools(
    wybin: WybinClient, data: WybinTournament
) -> tuple[dict[int, list[WybinBracket]], bool]:
    """Pools by wybin stage id: the ones embedded in the tournament, overridden by the mappool
    endpoint, and whether that endpoint could be read. If it is missing or unreadable the
    embedded ones are used and the caller reports it; a temporary wybin outage still fails the
    sync."""
    pools = {stage.wybin_stage_id: stage.brackets for stage in data.stages if stage.brackets}
    try:
        pools.update(await wybin.get_mappools(data.wybin_id))
    except (WybinNotFound, WybinFormatError) as error:
        logger.warning("wybin mappools unusable for tournament %s: %s", data.wybin_id, error)
        return pools, False
    return pools, True


async def _fetch_matches(wybin: WybinClient, data: WybinTournament) -> list[WybinMatch] | None:
    """The tournament's matches, or None when the endpoint is missing or not understood (the
    stored matches then stay as they are and the caller reports it). A temporary wybin outage
    still fails the sync."""
    try:
        return await wybin.get_matches(data.wybin_id)
    except (WybinNotFound, WybinFormatError) as error:
        logger.warning("wybin matches unusable for tournament %s: %s", data.wybin_id, error)
        return None


def _apply(
    conn: sqlite3.Connection,
    data: WybinTournament,
    pools: dict[int, list[WybinBracket]],
    matches: list[WybinMatch] | None,
    importer_osu_id: int | None,
    now: datetime,
    archive_if_ended: bool,
    pools_readable: bool = True,
) -> SyncResult:
    stamp = to_utc_iso(now)
    with transaction(conn):
        existing = tournaments_repo.find_by_wybin_id(conn, data.wybin_id)
        if existing is not None and existing.status == "archived":
            raise TournamentArchivedError(data.slug)
        shared = {
            "slug": data.slug,
            "name": data.name,
            "acronym": data.acronym,
            "gamemode": data.gamemode,
            "is_solo": int(data.is_solo),
            "team_size_min": data.team_size_min,
            "team_size_max": data.team_size_max,
        }
        if existing:
            tournament_id = existing.id
            tournaments_repo.update_from_wybin(conn, tournament_id, shared, stamp)
        else:
            deleted_repo.forget(conn, data.wybin_id, data.slug)  # imported again on purpose
            archived = archive_if_ended and _has_ended(data, now)
            tournament_id = tournaments_repo.insert(
                conn,
                {
                    **shared,
                    "wybin_id": data.wybin_id,
                    "owner_osu_id": importer_osu_id,
                    "source": "wybin",
                    "status": "archived" if archived else "active",
                    "archived_at": stamp if archived else None,
                    "last_synced_at": stamp,
                },
                stamp,
            )
        for sort_order, stage in enumerate(data.stages, 1):
            _apply_stage(conn, tournament_id, data.gamemode, stage, sort_order, pools)
        profiles = _apply_teams(conn, tournament_id, data)
        _assign_our_team(conn, tournament_id, importer_osu_id, data.is_solo, profiles, stamp)
        if matches is not None:
            apply_matches(conn, tournament_id, data.is_solo, matches, stamp)
    return SyncResult(tournament_id, existing is None, not pools_readable, matches is None)


def _has_ended(data: WybinTournament, now: datetime) -> bool:
    ends = [stage.end_at for stage in data.stages if stage.end_at]
    return bool(ends) and max(ends) < to_utc_iso(now - STALE_AFTER)


def _apply_stage(
    conn: sqlite3.Connection,
    tournament_id: int,
    gamemode: int,
    stage: WybinStage,
    sort_order: int,
    pools: dict[int, list[WybinBracket]],
) -> None:
    values: dict[str, Any] = {
        "name": stage.name,
        "sort_order": sort_order,
        "start_at": stage.start_at,
        "end_at": stage.end_at,
        "best_of": stage.best_of,
        "bans": stage.bans,
        "protects": stage.protects,
        "is_qualifier": int(stage.is_qualifier),
    }
    existing = stages_repo.find_by_wybin_id(conn, stage.wybin_stage_id)
    if existing and existing.tournament_id != tournament_id:
        return  # a stage id is owned by one tournament; never move it
    if existing:
        stage_id = existing.id
        stages_repo.update_from_wybin(conn, stage_id, values)
    else:
        stage_id = stages_repo.insert(
            conn, tournament_id, {**values, "wybin_stage_id": stage.wybin_stage_id}
        )
    brackets = pools.get(stage.wybin_stage_id)
    if brackets is None:
        return  # wybin sent no pool for this stage: unknown, so keep what is stored
    maps = build_pool(brackets, gamemode)
    stages_repo.replace_wybin_maps(conn, stage_id, maps)
    stages_repo.set_pool_released(conn, stage_id, bool(maps))


def build_pool(brackets: list[WybinBracket], gamemode: int) -> dict[str, dict[str, Any]]:
    """Pool rows by slot ("NM1", "HD2", "TB"). Placeholders are left out; maps are numbered by
    wybin's own order inside their bracket, and a clash with a taken slot moves to the next free
    number so two brackets with the same prefix cannot overwrite each other."""
    entries = [
        (bracket, beatmap)
        for bracket in brackets
        for beatmap in sorted(bracket.beatmaps, key=lambda item: item.order)
        if not beatmap.is_placeholder
    ]
    single_tiebreaker = sum(is_tiebreaker_bracket(b.name, b.mods) for b, _ in entries) == 1
    pool: dict[str, dict[str, Any]] = {}
    for sort_order, (bracket, beatmap) in enumerate(entries, 1):
        prefix = slot_prefix(bracket.name, bracket.mods)
        if prefix == TIEBREAKER_SLOT and single_tiebreaker:
            slot = prefix
        else:
            number = beatmap.order
            while f"{prefix}{number}" in pool:
                number += 1
            slot = f"{prefix}{number}"
        pool[slot] = _pool_row(bracket, beatmap, prefix, gamemode, sort_order)
    return pool


def _pool_row(
    bracket: WybinBracket, beatmap: WybinBeatmap, prefix: str, gamemode: int, sort_order: int
) -> dict[str, Any]:
    is_free = is_freemod_bracket(bracket.name, bracket.mods)
    is_tiebreaker = is_tiebreaker_bracket(bracket.name, bracket.mods)
    return {
        "bracket": bracket.name,
        "required_mods": _required_mods(beatmap, prefix, gamemode, is_free or is_tiebreaker),
        "is_freemod": int(is_free),
        "is_tiebreaker": int(is_tiebreaker),
        "sort_order": sort_order,
        "beatmap_id": beatmap.beatmap_id,
        "beatmapset_id": beatmap.beatmapset_id,
        "artist": beatmap.artist,
        "title": beatmap.title,
        "version": beatmap.version,
        "mapper": beatmap.mapper,
        "star_rating": beatmap.star_rating,
        "bpm": beatmap.bpm,
        "length_s": beatmap.length_s,
        "cs": beatmap.cs,
        "ar": beatmap.ar,
        "od": beatmap.od,
        "hp": beatmap.hp,
        "wybin_entry_id": beatmap.wybin_entry_id,
    }


def _required_mods(beatmap: WybinBeatmap, prefix: str, gamemode: int, is_free: bool) -> str:
    """Freemod and tiebreaker maps require nothing. Otherwise the map's own mod bitmask decides,
    and the bracket's name is the fallback when wybin leaves the bitmask empty."""
    if is_free:
        return ""
    from_bitmask = mods_from_bitmask(beatmap.mods_bitmask, gamemode)
    if from_bitmask or prefix in (NOMOD_SLOT, FREEMOD_SLOT, TIEBREAKER_SLOT):
        return from_bitmask
    try:
        return required_mods_for_prefix(prefix, gamemode)
    except ValueError:
        return ""


def _apply_teams(
    conn: sqlite3.Connection, tournament_id: int, data: WybinTournament
) -> dict[int, WybinPlayer]:
    """Create and refresh teams and their players. Returns every player seen, by osu! id."""
    profiles: dict[int, WybinPlayer] = {player.osu_id: player for player in data.players}
    if data.is_solo:
        for player in data.players:
            team_id = teams_repo.upsert_wybin_team(
                conn, tournament_id, player.wybin_player_id, player.username, None
            )
            teams_repo.replace_players(conn, team_id, [_player_values(player, False)])
        return profiles
    for team in data.teams:
        team_id = teams_repo.upsert_wybin_team(
            conn, tournament_id, team.wybin_team_id, team.name, team.seed
        )
        teams_repo.replace_players(
            conn,
            team_id,
            [
                _player_values(
                    member,
                    member.wybin_player_id == team.captain_player_id or member.is_temporary_captain,
                )
                for member in team.members
            ],
        )
        profiles.update({member.osu_id: member for member in team.members})
    return profiles


def _player_values(player: WybinPlayer, is_captain: bool) -> PlayerValues:
    return PlayerValues(
        player.osu_id,
        player.username,
        player.timezone_iana,
        player.timezone_offset_min,
        is_captain,
    )


def _assign_our_team(
    conn: sqlite3.Connection,
    tournament_id: int,
    importer_osu_id: int | None,
    is_solo: bool,
    profiles: dict[int, WybinPlayer],
    stamp: str,
) -> None:
    """Find our team if none is set yet, then add its roster as members.

    Our team is the one holding the importing admin, else one holding any linked admin. Once
    chosen (also by hand in the settings) it is kept. Captain roles are handed out only when the
    team is first assigned; later syncs add new players as plain players.
    """
    tournament = tournaments_repo.get(conn, tournament_id)
    if tournament is None:
        raise LookupError("tournament vanished during sync")
    team_id = tournament.our_team_id
    first_assignment = team_id is None
    if team_id is None:
        candidates = [importer_osu_id, tournament.owner_osu_id]
        candidates += [link.osu_id for link in links_repo.list_all(conn)]
        ordered = list(dict.fromkeys(osu_id for osu_id in candidates if osu_id is not None))
        team_id = teams_repo.find_team_with_player(conn, tournament_id, ordered)
        if team_id is None:
            return
        tournaments_repo.set_our_team(conn, tournament_id, team_id)
    add_roster_members(conn, tournament_id, team_id, is_solo, first_assignment, profiles, stamp)


def add_roster_members(
    conn: sqlite3.Connection,
    tournament_id: int,
    team_id: int,
    is_solo: bool,
    first_assignment: bool,
    profiles: dict[int, WybinPlayer],
    stamp: str,
) -> None:
    """Every player on the team becomes a member unless they already are one (D12)."""
    for player in teams_repo.list_players(conn, team_id):
        if members_repo.get(conn, tournament_id, player.osu_id):
            continue
        wybin_player = profiles.get(player.osu_id)
        users_repo.record_if_unknown(
            conn,
            OsuProfile(
                player.osu_id,
                player.username,
                wybin_player.avatar_url if wybin_player else None,
                wybin_player.country if wybin_player else None,
            ),
            stamp,
        )
        is_captain = first_assignment and (player.is_captain or is_solo)
        members_repo.add(
            conn, tournament_id, player.osu_id, "captain" if is_captain else "player", "wybin", None
        )
