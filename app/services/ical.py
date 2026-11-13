"""iCalendar (RFC 5545) text for the match feed. Hand-written: one VCALENDAR with one VEVENT per
match, times in UTC, lines folded at 75 octets and ended with CRLF. No I/O."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.slots import MATCH_DURATION

PRODID = "-//osu-tourney-hub//Match schedule//EN"
UID_DOMAIN = "osu-tourney-hub"
CRLF = "\r\n"
MAX_LINE_OCTETS = 75
_STAMP = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class CalendarEvent:
    match_id: int
    start: datetime
    summary: str
    modified: datetime  # when the match last changed; also the event's DTSTAMP


def escape_text(text: str) -> str:
    """A TEXT value: backslash, semicolon, comma and line breaks escaped; other control
    characters dropped."""
    kept = [char for char in text if char in "\r\n" or (ord(char) >= 0x20 and char != "\x7f")]
    return (
        "".join(kept)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """Break a content line into pieces of at most 75 octets (UTF-8), each continuation starting
    with one space. Never splits a multi-byte character."""
    pieces: list[str] = []
    current = ""
    size = 0
    limit = MAX_LINE_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            pieces.append(current)
            current, size, limit = "", 0, MAX_LINE_OCTETS - 1  # the leading space counts
        current += char
        size += width
    pieces.append(current)
    return (CRLF + " ").join(pieces)


def build_calendar(events: Iterable[CalendarEvent], name: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{escape_text(name)}",
    ]
    for event in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:match-{event.match_id}@{UID_DOMAIN}",
            f"DTSTAMP:{event.modified.astimezone(UTC).strftime(_STAMP)}",
            f"DTSTART:{event.start.astimezone(UTC).strftime(_STAMP)}",
            f"DTEND:{(event.start + MATCH_DURATION).astimezone(UTC).strftime(_STAMP)}",
            f"SUMMARY:{escape_text(event.summary)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(fold_line(line) + CRLF for line in lines)
