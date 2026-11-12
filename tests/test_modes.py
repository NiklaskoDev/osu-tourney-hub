import pytest

from app.domain.modes import (
    ALLOWED_MODS,
    CATCH,
    MANIA,
    STANDARD,
    TAIKO,
    bracket_for_prefix,
    canonical_mods,
    display_mods,
    is_freemod_bracket,
    is_tiebreaker_bracket,
    is_valid_mode,
    mode_name,
    mods_from_bitmask,
    required_mods_for_prefix,
    slot_prefix,
    split_mods,
)

# --- canonical form ---


def test_canonical_mods_are_sorted_and_joined():
    assert canonical_mods(["HR", "HD"], STANDARD) == "HDHR"
    assert canonical_mods(["hd", "dt"], STANDARD) == "DTHD"


def test_no_fail_and_nomod_are_dropped():
    assert canonical_mods(["NF"], STANDARD) == ""
    assert canonical_mods(["NM"], STANDARD) == ""
    assert canonical_mods(["HD", "NF"], STANDARD) == "HD"
    assert canonical_mods([], STANDARD) == ""


def test_nightcore_replaces_double_time_and_perfect_replaces_sudden_death():
    assert canonical_mods(["DT", "NC"], STANDARD) == "NC"
    assert canonical_mods(["SD", "PF", "HD"], STANDARD) == "HDPF"


def test_duplicates_collapse():
    assert canonical_mods(["HD", "HD"], STANDARD) == "HD"


@pytest.mark.parametrize(
    ("mode", "acronym"),
    [
        (MANIA, "HR"),  # mania has no Hard Rock
        (MANIA, "RX"),
        (MANIA, "SO"),
        (STANDARD, "K4"),  # key mods only exist in mania
        (STANDARD, "FI"),
        (TAIKO, "TD"),
        (CATCH, "AP"),
        (CATCH, "K7"),
        (STANDARD, "XX"),
    ],
)
def test_a_mod_the_mode_does_not_have_is_refused(mode, acronym):
    with pytest.raises(ValueError, match=acronym):
        canonical_mods([acronym], mode)


@pytest.mark.parametrize(
    ("mode", "acronyms", "expected"),
    [
        (MANIA, ["K4", "HD"], "HDK4"),
        (MANIA, ["FI", "MR"], "FIMR"),
        (STANDARD, ["TD", "HD"], "HDTD"),
        (STANDARD, ["AP"], "AP"),
        (TAIKO, ["HR", "DT"], "DTHR"),
        (CATCH, ["HR", "FL"], "FLHR"),
    ],
)
def test_each_mode_accepts_its_own_mods(mode, acronyms, expected):
    assert canonical_mods(acronyms, mode) == expected


def test_every_mode_table_only_holds_known_mods():
    from app.domain.modes import MOD_BITS

    for mode, allowed in ALLOWED_MODS.items():
        assert allowed <= set(MOD_BITS), mode_name(mode)


# --- bitmask ---


def test_bitmask_to_canonical_mods():
    assert mods_from_bitmask(0, STANDARD) == ""
    assert mods_from_bitmask(8, CATCH) == "HD"
    assert mods_from_bitmask(16, CATCH) == "HR"
    assert mods_from_bitmask(64, CATCH) == "DT"
    assert mods_from_bitmask(8 | 16, STANDARD) == "HDHR"


def test_the_nightcore_bitmask_sets_both_bits_but_reads_as_nightcore():
    assert mods_from_bitmask(64 | 512, STANDARD) == "NC"
    assert mods_from_bitmask(32 | 16384, STANDARD) == "PF"


def test_bitmask_ignores_no_fail_and_bits_the_mode_lacks():
    assert mods_from_bitmask(1 | 8, STANDARD) == "HD"
    assert mods_from_bitmask(16, MANIA) == ""  # Hard Rock bit means nothing in mania
    assert mods_from_bitmask(2**40, STANDARD) == ""


def test_mania_key_bitmask():
    assert mods_from_bitmask(32768, MANIA) == "K4"
    assert mods_from_bitmask(32768 | 8, MANIA) == "HDK4"
    assert mods_from_bitmask(32768, STANDARD) == ""


# --- strings ---


def test_split_mods():
    assert split_mods("HDHR") == ["HD", "HR"]
    assert split_mods(" dt ") == ["DT"]
    assert split_mods("") == []
    with pytest.raises(ValueError, match="two-letter"):
        split_mods("HDH")


def test_display_mods_shows_nm_for_nothing():
    assert display_mods("") == "NM"
    assert display_mods("HDHR") == "HDHR"


@pytest.mark.parametrize(
    ("prefix", "mode", "expected"),
    [
        ("NM", STANDARD, ""),
        ("FM", STANDARD, ""),
        ("TB", STANDARD, ""),
        ("HD", STANDARD, "HD"),
        ("HDHR", STANDARD, "HDHR"),
        ("K4", MANIA, "K4"),
    ],
)
def test_required_mods_follow_the_slot_prefix(prefix, mode, expected):
    assert required_mods_for_prefix(prefix, mode) == expected


def test_a_slot_prefix_with_an_unknown_mod_is_refused():
    with pytest.raises(ValueError):
        required_mods_for_prefix("ZZ", STANDARD)
    with pytest.raises(ValueError):
        required_mods_for_prefix("HR", MANIA)


# --- brackets ---


@pytest.mark.parametrize(
    ("name", "mods", "prefix"),
    [
        ("NoMod", "None", "NM"),
        ("NoFail", None, "NM"),
        ("Hidden", "Hidden", "HD"),
        ("HardRock", None, "HR"),
        ("DoubleTime", None, "DT"),
        ("Freemod", None, "FM"),
        ("MixedMod", "MixedMod", "FM"),
        ("Tiebreaker", "Tiebreaker", "TB"),
        ("hidden", None, "HD"),
        ("Easy Mod", None, "EM"),  # unknown name: capital letters
        ("Rice", None, "RI"),  # unknown single word: first two letters
        ("Custom bracket", "Hidden", "HD"),  # the mods text names it when the name does not
    ],
)
def test_bracket_names_map_to_slot_prefixes(name, mods, prefix):
    assert slot_prefix(name, mods) == prefix


def test_freemod_and_tiebreaker_brackets_are_recognised():
    assert is_freemod_bracket("Freemod", None)
    assert is_freemod_bracket("MixedMod", "MixedMod")
    assert not is_freemod_bracket("NoMod", "None")
    assert is_tiebreaker_bracket("Tiebreaker", "Tiebreaker")
    assert not is_tiebreaker_bracket("Hidden", "Hidden")


def test_bracket_label_round_trips_for_the_standard_prefixes():
    for prefix in ("NM", "HD", "HR", "DT", "FM", "TB"):
        assert slot_prefix(bracket_for_prefix(prefix)) == prefix
    assert bracket_for_prefix("EZ") == "EZ"


def test_mode_helpers():
    assert mode_name(2) == "osu!catch"
    assert mode_name(9) == "Unknown mode"
    assert is_valid_mode(3)
    assert not is_valid_mode(4)
    assert not is_valid_mode(-1)
