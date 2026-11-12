"""osu! game modes and mods: which mods exist per mode, bitmask <-> acronym, and the canonical
form stored in the database. No I/O.

Canonical mods are the acronyms sorted alphabetically and joined without a separator ("DTHD").
`NF` is dropped because tournaments ignore it, and the empty string means no mods (shown as NM).
"""

from collections.abc import Iterable

STANDARD, TAIKO, CATCH, MANIA = 0, 1, 2, 3
GAME_MODES = {STANDARD: "osu!", TAIKO: "osu!taiko", CATCH: "osu!catch", MANIA: "osu!mania"}

# osu! stable bitmask values, the format wybin uses for a map's required mods.
MOD_BITS = {
    "NF": 1,
    "EZ": 2,
    "TD": 4,
    "HD": 8,
    "HR": 16,
    "SD": 32,
    "DT": 64,
    "RX": 128,
    "HT": 256,
    "NC": 512,
    "FL": 1024,
    "SO": 4096,
    "AP": 8192,
    "PF": 16384,
    "K4": 32768,
    "K5": 65536,
    "K6": 131072,
    "K7": 262144,
    "K8": 524288,
    "FI": 1048576,
    "RD": 2097152,
    "K9": 16777216,
    "K1": 67108864,
    "K3": 134217728,
    "K2": 268435456,
    "V2": 536870912,
    "MR": 1073741824,
}

_COMMON = ("NF", "EZ", "HD", "SD", "DT", "HT", "NC", "FL", "PF", "V2")
_MANIA_KEYS = ("K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9")
ALLOWED_MODS: dict[int, frozenset[str]] = {
    STANDARD: frozenset((*_COMMON, "HR", "RX", "TD", "SO", "AP")),
    TAIKO: frozenset((*_COMMON, "HR", "RX")),
    CATCH: frozenset((*_COMMON, "HR", "RX")),
    # Mania has no Hard Rock, Relax, Auto-pilot, Spun-out or touch device, but has key mods.
    MANIA: frozenset((*_COMMON, *_MANIA_KEYS, "FI", "RD", "MR")),
}

IGNORED_MODS = frozenset({"NF"})
NO_MOD_LABEL = "NM"
FREEMOD_SLOT = "FM"
TIEBREAKER_SLOT = "TB"
NOMOD_SLOT = "NM"
_ACRONYM_LENGTH = 2

# Bracket names (lower case, letters and digits only) -> slot prefix.
_BRACKET_PREFIXES = {
    "nomod": NOMOD_SLOT,
    "none": NOMOD_SLOT,
    "nofail": NOMOD_SLOT,
    "nm": NOMOD_SLOT,
    "hidden": "HD",
    "hd": "HD",
    "hardrock": "HR",
    "hr": "HR",
    "doubletime": "DT",
    "dt": "DT",
    "easy": "EZ",
    "halftime": "HT",
    "flashlight": "FL",
    "freemod": FREEMOD_SLOT,
    "fm": FREEMOD_SLOT,
    "mixedmod": FREEMOD_SLOT,
    "mixed": FREEMOD_SLOT,
    "tiebreaker": TIEBREAKER_SLOT,
    "tb": TIEBREAKER_SLOT,
}

# Bracket names wybin uses, for pools entered by hand.
_PREFIX_BRACKETS = {
    NOMOD_SLOT: "NoMod",
    "HD": "Hidden",
    "HR": "HardRock",
    "DT": "DoubleTime",
    FREEMOD_SLOT: "Freemod",
    TIEBREAKER_SLOT: "Tiebreaker",
}


def mode_name(gamemode: int) -> str:
    return GAME_MODES.get(gamemode, "Unknown mode")


def is_valid_mode(gamemode: int) -> bool:
    return gamemode in GAME_MODES


def canonical_mods(acronyms: Iterable[str], gamemode: int) -> str:
    """Normalise a set of mods for a mode.

    Raises `ValueError` for an acronym the mode does not have. NC replaces DT and PF replaces SD
    (the game sets both bits), NF is dropped and "NM" counts as no mod.
    """
    allowed = ALLOWED_MODS[gamemode]
    found: set[str] = set()
    for raw in acronyms:
        acronym = raw.strip().upper()
        if acronym in IGNORED_MODS or acronym == NO_MOD_LABEL or not acronym:
            continue
        if acronym not in allowed:
            raise ValueError(f"{acronym} is not a mod in {mode_name(gamemode)}")
        found.add(acronym)
    if "NC" in found:
        found.discard("DT")
    if "PF" in found:
        found.discard("SD")
    return "".join(sorted(found))


def split_mods(text: str) -> list[str]:
    """Split a concatenated acronym string ("HDHR") into acronyms. Raises `ValueError`."""
    value = text.strip().upper()
    if len(value) % _ACRONYM_LENGTH:
        raise ValueError(f"{text!r} is not a list of two-letter mod acronyms")
    return [value[i : i + _ACRONYM_LENGTH] for i in range(0, len(value), _ACRONYM_LENGTH)]


def mods_from_bitmask(bitmask: int, gamemode: int) -> str:
    """Canonical mods of an osu! stable bitmask. Bits the mode does not have are ignored."""
    allowed = ALLOWED_MODS[gamemode]
    return canonical_mods(
        (name for name, bit in MOD_BITS.items() if bitmask & bit and name in allowed), gamemode
    )


def display_mods(canonical: str) -> str:
    return canonical or NO_MOD_LABEL


def slot_prefix(bracket_name: str, bracket_mods: str | None = None) -> str:
    """Slot prefix for a mod bracket: "Hidden" -> HD, "Freemod" -> FM, "Tiebreaker" -> TB.

    Unknown names fall back to the capital letters of the name, or its first two letters.
    """
    for candidate in (bracket_name, bracket_mods or ""):
        known = _BRACKET_PREFIXES.get(_squash(candidate))
        if known:
            return known
    capitals = "".join(char for char in bracket_name if char.isupper())
    if len(capitals) >= _ACRONYM_LENGTH:
        return capitals[:4]
    letters = "".join(char for char in bracket_name if char.isalpha()).upper()
    return letters[:_ACRONYM_LENGTH] or NOMOD_SLOT


def is_freemod_bracket(bracket_name: str, bracket_mods: str | None) -> bool:
    return slot_prefix(bracket_name, bracket_mods) == FREEMOD_SLOT


def is_tiebreaker_bracket(bracket_name: str, bracket_mods: str | None) -> bool:
    return slot_prefix(bracket_name, bracket_mods) == TIEBREAKER_SLOT


def required_mods_for_prefix(prefix: str, gamemode: int) -> str:
    """Canonical required mods implied by a slot prefix: NM, FM and TB need none; "HD" needs HD;
    "HDHR" needs both. Raises `ValueError` for anything the mode does not have."""
    if prefix in (NOMOD_SLOT, FREEMOD_SLOT, TIEBREAKER_SLOT):
        return ""
    return canonical_mods(split_mods(prefix), gamemode)


def bracket_for_prefix(prefix: str) -> str:
    return _PREFIX_BRACKETS.get(prefix, prefix)


def _squash(text: str) -> str:
    return "".join(char for char in text.lower() if char.isalnum())
