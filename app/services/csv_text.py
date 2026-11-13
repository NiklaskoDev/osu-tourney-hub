"""CSV text handling shared by export, upload and the Google Sheets fetch: size limits, decoding,
reading a table, and keeping spreadsheet programs from running a cell as a formula."""

import csv
import io
from dataclasses import dataclass

MAX_CSV_BYTES = 1_048_576  # 1 MB
MAX_CSV_ROWS = 5000
DELIMITERS = (",", ";", "\t")  # Excel in many locales writes semicolons
# A cell starting with one of these is a formula to Excel and Sheets.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_ESCAPE = "'"


class CsvImportError(Exception):
    """The file or link cannot be imported; the message is safe to show."""


@dataclass(frozen=True)
class CsvTable:
    headers: list[str]
    rows: list[list[str]]
    lines: list[int]  # the line of the file each row starts on (the header is line 1)


def decode_upload(data: bytes) -> str:
    """The text of an uploaded file. Raises `CsvImportError` when it is too large or not UTF-8."""
    if len(data) > MAX_CSV_BYTES:
        raise CsvImportError("The file is larger than 1 MB. Import fewer rows.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CsvImportError("The file is not UTF-8 text. Save it as CSV (UTF-8).") from error


def parse_table(text: str) -> CsvTable:
    """Read the first non-empty row as headers and the rest as data rows.

    Raises `CsvImportError` for text over the limits, without a header or with no data rows.
    """
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise CsvImportError("The file is larger than 1 MB. Import fewer rows.")
    if "\x00" in text:  # binary data, not a spreadsheet export
        raise CsvImportError("The file is not a readable CSV.")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text))
    headers: list[str] | None = None
    rows: list[list[str]] = []
    lines: list[int] = []
    previous_end = 0
    try:
        for row in reader:
            start, previous_end = previous_end + 1, reader.line_num
            if not any(cell.strip() for cell in row):
                continue
            if headers is None:
                headers = [cell.strip() for cell in row]
                continue
            if len(rows) >= MAX_CSV_ROWS:
                raise CsvImportError(f"The file has more than {MAX_CSV_ROWS} rows.")
            rows.append(row)
            lines.append(start)
    except csv.Error as error:
        raise CsvImportError("The file is not a readable CSV.") from error
    if headers is None or not rows:
        raise CsvImportError("The file has no rows to import. The first row must name the columns.")
    return CsvTable(headers, rows, lines)


def guard_cell(text: str) -> str:
    """Export: make a spreadsheet show a formula-looking cell as text."""
    return _ESCAPE + text if text.startswith(_FORMULA_STARTS) else text


def clean_cell(text: str) -> str:
    """Import: trim a cell and undo `guard_cell`."""
    value = text.strip()
    if len(value) > 1 and value.startswith(_ESCAPE) and value[1:].startswith(_FORMULA_STARTS):
        return value[1:]
    return value


def _delimiter(text: str) -> str:
    first_line = text.lstrip().split("\n", 1)[0]
    counts = {delimiter: first_line.count(delimiter) for delimiter in DELIMITERS}
    best = max(counts, key=lambda delimiter: counts[delimiter])
    return best if counts[best] else ","
