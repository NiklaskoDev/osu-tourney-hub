"""What goes to an AI provider and what comes back. Plain data, no I/O.

The two teams of a match are always "A" (the left team of the match page) and "B" (the right one),
so the answer does not depend on which names were shared.
"""

from dataclasses import dataclass

SIDE_A, SIDE_B = "A", "B"
SIDES = (SIDE_A, SIDE_B)
MAX_REASONING_CHARS = 600


@dataclass(frozen=True)
class PredictionContext:
    """The prompt data as JSON-ready values, and the pool slots an answer may name."""

    data: dict
    slots: frozenset[str]


@dataclass(frozen=True)
class AiPrediction:
    """A provider's answer, already checked against the context it was given."""

    winner: str  # "A" or "B"
    win_percent: float  # the winner's chance, 50 to 100
    bans: dict[str, tuple[str, ...]]  # side -> pool slots
    picks: tuple[tuple[str, str], ...]  # (side, pool slot) in pick order
    reasoning: str
