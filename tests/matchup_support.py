"""A tournament with two teams, scores and matches, shared by the match page and matchup tests."""

from tests.support import (
    add_match,
    add_member,
    add_pool_map,
    add_score,
    add_stage,
    add_team,
    add_tournament,
)

CAPTAIN, MATE, SUB, GUEST = 2001, 2002, 2003, 2004
RIVAL_A, RIVAL_B = 3001, 3002
NM1, NM2, HD1, TB = 1001, 1002, 1003, 1004


def build_cup(db):
    """Alpha Squad (ours: captain, mate, a substitute and a guest) against Rival Crew; a best-of-3
    stage with one ban each and lineup size 2; maps NM1, NM2, HD1 and a tiebreaker."""
    tid = add_tournament(db, "Spring Cup", acronym="SC")
    ours = add_team(
        db,
        tid,
        "Alpha Squad",
        [(CAPTAIN, "Cap"), (MATE, "Mate"), (SUB, "Sub"), (GUEST, "Guest")],
        ours=True,
    )
    rivals = add_team(db, tid, "Rival Crew", [(RIVAL_A, "RivalA"), (RIVAL_B, "RivalB")])
    add_member(db, tid, CAPTAIN, "captain")
    add_member(db, tid, MATE, "player")
    add_member(db, tid, SUB, "player", is_substitute=True)
    add_member(db, tid, GUEST, "guest")
    stage = add_stage(db, tid, "Round of 16", lineup_size=2)
    db.execute("UPDATE stages SET best_of = 3, bans = 1, protects = 0 WHERE id = ?", (stage,))
    maps = {
        "NM1": add_pool_map(db, stage, "NM1", beatmap_id=NM1),
        "NM2": add_pool_map(db, stage, "NM2", beatmap_id=NM2),
        "HD1": add_pool_map(db, stage, "HD1", beatmap_id=HD1, required_mods="HD"),
        "TB": add_pool_map(db, stage, "TB", beatmap_id=TB, tiebreaker=True),
    }
    return {"id": tid, "ours": ours, "rivals": rivals, "stage": stage, "maps": maps}


def play(db, beatmap, player, points, mods=""):
    add_score(db, beatmap, player, points, mods=mods)


def fill_scores(db, ours=1_000_000, theirs=800_000):
    """Both teams have scored on all maps; `ours` and `theirs` are the score of each player."""
    for beatmap, mods in ((NM1, ""), (NM2, ""), (HD1, "HD"), (TB, "")):
        for player in (CAPTAIN, MATE):
            play(db, beatmap, player, ours, mods)
        for player in (RIVAL_A, RIVAL_B):
            play(db, beatmap, player, theirs, mods)


def add_game(db, cup, ours_first=True, when="2026-11-14T18:00:00Z", **extra):
    """A match of our team against the rivals in the cup's stage."""
    first, second = (cup["ours"], cup["rivals"]) if ours_first else (cup["rivals"], cup["ours"])
    return add_match(db, cup["id"], first, second, when, stage_id=cup["stage"], **extra)
