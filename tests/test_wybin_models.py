import copy

import pytest

from app.clients.wybin_models import (
    WybinFormatError,
    ms_to_utc_iso,
    parse_mappool_stages,
    parse_timezone_offset,
    parse_tournament,
    parse_user_detail,
    split_map_name,
)
from tests.support import load_fixture


@pytest.fixture
def team_data() -> dict:
    return load_fixture("tournament_team")


# --- tournament ---


def test_a_team_tournament_is_parsed(team_data):
    tournament = parse_tournament(team_data)

    assert (tournament.wybin_id, tournament.slug, tournament.name) == (
        9001,
        "synthetic-cup",
        "Synthetic Catch Cup",
    )
    assert tournament.acronym == "SCC"
    assert tournament.gamemode == 2
    assert not tournament.is_solo
    assert (tournament.team_size_min, tournament.team_size_max) == (3, 6)
    assert len(tournament.teams) == 3
    assert len(tournament.players) == 11


def test_stages_are_sorted_by_start_date_not_by_wybin_order(team_data):
    tournament = parse_tournament(team_data)

    assert [stage.name for stage in tournament.stages] == ["Qualifiers", "Round of 16", "Finals"]
    assert tournament.stages[0].start_at == "2026-11-20T12:00:00Z"
    assert tournament.stages[0].end_at == "2026-11-21T12:00:00Z"


def test_stage_format_and_qualifier_flag(team_data):
    qualifiers, round_of_16, finals = parse_tournament(team_data).stages

    assert qualifiers.is_qualifier
    assert (qualifiers.best_of, qualifiers.bans, qualifiers.protects) == (None, None, None)
    assert not round_of_16.is_qualifier
    assert (round_of_16.best_of, round_of_16.bans, round_of_16.protects) == (9, 1, None)
    assert (finals.best_of, finals.bans, finals.protects) == (13, 2, 1)


def test_a_stage_without_modbrackets_has_an_unknown_pool(team_data):
    assert all(stage.brackets is None for stage in parse_tournament(team_data).stages)


def test_team_members_captain_and_timezones(team_data):
    team = parse_tournament(team_data).teams[0]

    assert team.name == "Alpha Squad"
    assert team.seed == 3
    assert team.captain_player_id == 12
    members = {member.osu_id: member for member in team.members}
    assert members[1001].timezone_iana == "Europe/Berlin"
    assert members[3001].timezone_offset_min == -360
    assert members[3002].timezone_offset_min == 330
    assert members[3003].is_temporary_captain
    assert not members[3001].is_temporary_captain


def test_a_team_without_a_seed_has_none(team_data):
    assert parse_tournament(team_data).teams[2].seed is None


def test_discord_ids_and_other_unknown_keys_are_ignored(team_data):
    team_data["players"][0]["discordId"] = "123"
    team_data["extra"] = {"anything": 1}

    parsed = parse_tournament(team_data)

    assert not any("discord" in key for key in vars(parsed.players[0]))


def test_a_solo_tournament_is_detected_from_the_flag_or_a_team_size_of_one(team_data):
    team_data["soloTournament"] = True
    assert parse_tournament(team_data).is_solo
    team_data["soloTournament"] = False
    team_data["maximumTeamSize"] = 1
    assert parse_tournament(team_data).is_solo
    team_data["maximumTeamSize"] = 4
    assert not parse_tournament(team_data).is_solo


def test_avatar_urls_outside_the_osu_image_hosts_are_dropped(team_data):
    team_data["players"][0]["user"]["userOsu"]["avatar_url"] = "https://evil.example/a.png"

    assert parse_tournament(team_data).players[0].avatar_url is None
    assert parse_tournament(load_fixture("tournament_team")).players[0].avatar_url


@pytest.mark.parametrize("key", ["id", "name", "slug", "gamemode"])
def test_a_missing_required_field_is_a_format_error(team_data, key):
    del team_data[key]

    with pytest.raises(WybinFormatError):
        parse_tournament(team_data)


def test_game_mode_zero_is_osu_standard(team_data):
    team_data["gamemode"] = 0

    assert parse_tournament(team_data).gamemode == 0


def test_an_unknown_game_mode_is_a_format_error(team_data):
    team_data["gamemode"] = 7

    with pytest.raises(WybinFormatError, match="game mode"):
        parse_tournament(team_data)


@pytest.mark.parametrize("data", [[], "text", None, 5])
def test_a_response_that_is_not_an_object_is_a_format_error(data):
    with pytest.raises(WybinFormatError):
        parse_tournament(data)


def test_stages_that_are_not_a_list_are_a_format_error(team_data):
    team_data["stages"] = {"id": 1}

    with pytest.raises(WybinFormatError):
        parse_tournament(team_data)


def test_missing_lists_count_as_empty(team_data):
    del team_data["teams"]
    del team_data["players"]

    parsed = parse_tournament(team_data)

    assert parsed.teams == [] and parsed.players == []


def test_one_player_without_an_osu_profile_is_skipped(team_data):
    broken = copy.deepcopy(team_data["players"][0])
    broken["user"]["userOsu"] = None
    team_data["players"].append(broken)

    assert len(parse_tournament(team_data).players) == 11


def test_a_player_list_where_nobody_can_be_read_is_a_format_error(team_data):
    for player in team_data["players"]:
        player["user"] = None

    with pytest.raises(WybinFormatError, match="player"):
        parse_tournament(team_data)


# --- mappool ---


def test_pools_are_returned_by_stage_and_unknown_pools_are_left_out():
    pools = parse_mappool_stages(load_fixture("mappools"))

    assert set(pools) == {701, 702}  # stage 703 sent modBrackets null
    assert [bracket.name for bracket in pools[701]] == [
        "NoMod",
        "Hidden",
        "HardRock",
        "DoubleTime",
        "MixedMod",
        "Tiebreaker",
    ]


def test_beatmap_fields_and_name_split():
    beatmap = parse_mappool_stages(load_fixture("mappools"))[701][0].beatmaps[1]

    assert beatmap.beatmap_id == 910001
    assert beatmap.beatmapset_id == 911001
    assert (beatmap.artist, beatmap.title, beatmap.version) == (
        "Synth Artist",
        "First Song",
        "Rain",
    )
    assert beatmap.mapper == "MapperMia"
    assert beatmap.order == 1
    assert beatmap.star_rating == 6.5
    assert beatmap.length_s == 180
    assert not beatmap.is_placeholder


def test_the_mod_bitmask_and_bracket_mods_text_are_kept():
    hidden = parse_mappool_stages(load_fixture("mappools"))[701][1]

    assert hidden.mods == "Hidden"
    assert {beatmap.mods_bitmask for beatmap in hidden.beatmaps} == {8}


def test_a_placeholder_map_is_flagged():
    placeholder = parse_mappool_stages(load_fixture("mappools"))[702][0].beatmaps[0]

    assert placeholder.is_placeholder
    assert placeholder.beatmap_id is None


def test_a_map_name_without_the_usual_shape_becomes_the_title():
    assert split_map_name("Just a name") == (None, "Just a name", None)
    assert split_map_name("A - B [C]") == ("A", "B", "C")
    assert split_map_name("Artist - Title - Part [Hard]") == ("Artist", "Title - Part", "Hard")
    assert split_map_name(None) == (None, None, None)
    assert split_map_name("") == (None, None, None)


# --- user detail ---


def test_sign_ups_are_read_from_participant_tournaments():
    detail = parse_user_detail(load_fixture("user_detailed"))

    assert [(ref.wybin_id, ref.slug) for ref in detail.tournaments] == [
        (9001, "synthetic-cup"),
        (9002, "synthetic-solo"),
    ]


def test_sign_ups_nested_under_a_tournament_key_are_read_too():
    detail = parse_user_detail(
        {"participantTournaments": [{"tournament": {"id": 5, "slug": "nested"}}, "junk", 7]}
    )

    assert [(ref.wybin_id, ref.slug) for ref in detail.tournaments] == [(5, "nested")]


def test_a_user_without_sign_ups_has_none():
    assert parse_user_detail({"id": 1}).tournaments == []


# --- helpers ---


def test_milliseconds_become_utc_text():
    assert ms_to_utc_iso(1_700_000_000_000) == "2023-11-14T22:13:20Z"
    assert ms_to_utc_iso(None) is None
    assert ms_to_utc_iso("x") is None
    assert ms_to_utc_iso(True) is None
    assert ms_to_utc_iso(10**30) is None


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("UTC+8", 480),
        ("UTC-6", -360),
        ("UTC+5:30", 330),
        ("UTC+05:30", 330),
        ("UTC-3:30", -210),
        ("UTC", 0),
        ("GMT+2", 120),
        ("+1", 60),
        ("UTC+15", None),  # beyond any real zone
        ("Europe/Berlin", None),
        ("", None),
        (None, None),
        (5, None),
    ],
)
def test_timezone_offsets(text, minutes):
    assert parse_timezone_offset(text) == minutes
