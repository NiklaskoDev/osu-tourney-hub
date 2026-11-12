"""Parsing of numbers typed into forms. No I/O."""

# Python refuses to turn more than 4300 digits into an int, and no id or count in this app has more
# than ten digits, so longer input is rejected before it reaches `int()`.
MAX_DIGITS = 10


def parse_uint(text: str) -> int | None:
    """A whole number made of 1 to 10 ASCII digits, or None for anything else (empty, signs,
    decimals, other scripts' digits, very long input)."""
    value = text.strip()
    if not value or len(value) > MAX_DIGITS or not (value.isascii() and value.isdecimal()):
        return None
    return int(value)
