"""Store the matches wybin lists for a tournament, naming each side's team and the stage.

Part of the wybin sync, so it runs inside its transaction and only adds or refreshes. What wybin
puts under `playerX` and `opponentX` is confirmed for 1v1 tournaments only; a side is matched to a
team by whatever ids it carries (see `WybinSide`), and a side that matches nothing stays empty
rather than being guessed.
"""

import sqlite3

from app.clients.wybin_models import WybinMatch, WybinSide
from app.repos import matches as matches_repo
from app.repos import stages as stages_repo
from app.repos import teams as teams_repo
from app.repos.matches import MatchValues
from app.services import links

STATUS_SCHEDULED = "scheduled"
STATUS_DONE = "done"


def apply_matches(
    conn: sqlite3.Connection,
    tournament_id: int,
    is_solo: bool,
    matches: list[WybinMatch],
    stamp: str,
) -> None:
    lookup = _TeamLookup.load(conn, tournament_id)
    for match in matches:
        stage = (
            stages_repo.find_by_wybin_id(conn, match.wybin_stage_id)
            if match.wybin_stage_id is not None
            else None
        )
        values = MatchValues(
            wybin_match_id=match.wybin_match_id,
            stage_id=stage.id if stage and stage.tournament_id == tournament_id else None,
            label=match.label,
            team1_id=lookup.resolve(match.side1, is_solo),
            team2_id=lookup.resolve(match.side2, is_solo),
            scheduled_at=match.scheduled_at,
            score1=match.score1,
            score2=match.score2,
            mp_match_id=links.match_id(match.multiplayer_link) if match.multiplayer_link else None,
            status=status_for(match),
        )
        matches_repo.upsert_wybin(conn, tournament_id, values, stamp)


def status_for(match: WybinMatch) -> str:
    """Done once wybin has a result. 0-0 is how an unplayed match looks, so it is not a result."""
    played = (
        match.score1 is not None and match.score2 is not None and bool(match.score1 or match.score2)
    )
    return STATUS_DONE if played else STATUS_SCHEDULED


class _TeamLookup:
    def __init__(self, by_wybin_id: dict[int, int], by_osu_id: dict[int, int]) -> None:
        self._by_wybin_id = by_wybin_id
        self._by_osu_id = by_osu_id

    @classmethod
    def load(cls, conn: sqlite3.Connection, tournament_id: int) -> "_TeamLookup":
        by_wybin_id: dict[int, int] = {}
        by_osu_id: dict[int, int] = {}
        for team in teams_repo.list_for_tournament(conn, tournament_id):
            if team.wybin_team_id is not None:
                by_wybin_id[team.wybin_team_id] = team.id
            for player in teams_repo.list_players(conn, team.id):
                by_osu_id[player.osu_id] = team.id
        return cls(by_wybin_id, by_osu_id)

    def resolve(self, side: WybinSide, is_solo: bool) -> int | None:
        """A solo entry is one player, so the osu! id is the surest key and wybin's player id
        (the solo team's wybin id) the next. A team side is named by `opponentX`; a player id
        found under `playerX` must not be read as a team id because the two id spaces overlap."""
        if is_solo:
            return self._first(self._by_osu_id, side.osu_ids) or self._first(
                self._by_wybin_id, side.player_ids + side.opponent_ids
            )
        return self._first(self._by_wybin_id, side.opponent_ids) or self._first(
            self._by_osu_id, side.osu_ids
        )

    @staticmethod
    def _first(table: dict[int, int], keys: tuple[int, ...]) -> int | None:
        return next((table[key] for key in keys if key in table), None)
