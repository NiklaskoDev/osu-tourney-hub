"""Fetch a public Google Sheets link as CSV (D7).

Only `https://docs.google.com/spreadsheets/d/<id>` links are accepted, and the request goes to
`docs.google.com` and nowhere else: the address is rebuilt from the sheet id (and tab), never taken
as typed, and a redirect is only followed when it stays on that host. There is no way to make the
app fetch an arbitrary address.
"""

import re
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from app.services.csv_text import MAX_CSV_BYTES, CsvImportError

SHEETS_HOST = "docs.google.com"
MAX_REDIRECTS = 3
_SHEET_PATH = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{10,100})(?:/|$)")
_GID = re.compile(r"\d{1,10}")
_GID_IN_FRAGMENT = re.compile(r"(?:^|&)gid=([^&]*)")

BAD_LINK = "Paste the link of a Google Sheet, like https://docs.google.com/spreadsheets/d/..."
NOT_SHARED = (
    "Google did not hand out the sheet. Share it with 'Anyone with the link' as viewer and "
    "try again."
)
UNREACHABLE = "Google did not answer. Try again later."


def export_url(link: str) -> str:
    """The CSV address for a pasted sheet link. Raises `CsvImportError` for anything else."""
    parts = urlsplit(link.strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    found = _SHEET_PATH.match(parts.path)
    if (
        parts.scheme != "https"
        or parts.hostname != SHEETS_HOST
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or found is None
    ):
        raise CsvImportError(BAD_LINK)
    url = f"https://{SHEETS_HOST}/spreadsheets/d/{found.group(1)}/export?format=csv"
    gid = _gid(parts.query, parts.fragment)
    return url if gid is None else f"{url}&gid={gid}"


async def fetch_sheet_csv(http: httpx.AsyncClient, link: str) -> str:
    """Download the sheet's first tab (or the one in the link) as text."""
    url = export_url(link)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            async with http.stream("GET", url, headers={"Accept": "text/csv"}) as response:
                if response.is_redirect:
                    url = _same_host(urljoin(url, response.headers.get("location", "")))
                    continue
                return await _read(response)
        except httpx.HTTPError as error:
            raise CsvImportError(UNREACHABLE) from error
    raise CsvImportError(NOT_SHARED)


async def _read(response: httpx.Response) -> str:
    if response.status_code in (401, 403):
        raise CsvImportError(NOT_SHARED)
    if response.status_code == 404:
        raise CsvImportError("Google could not find this sheet. Check the link.")
    if response.status_code != 200:
        raise CsvImportError(UNREACHABLE)
    if response.headers.get("content-type", "").lower().startswith("text/html"):
        raise CsvImportError(NOT_SHARED)  # a sign-in page, not the sheet
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_CSV_BYTES:
            raise CsvImportError("The sheet is larger than 1 MB. Import fewer rows.")
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CsvImportError("The sheet is not UTF-8 text.") from error


def _same_host(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != SHEETS_HOST:
        raise CsvImportError(NOT_SHARED)  # usually Google's sign-in page for a private sheet
    return url


def _gid(query: str, fragment: str) -> str | None:
    """The tab number from `?gid=` or `#gid=`; None when the link names no tab."""
    values = parse_qs(query).get("gid")
    raw = values[0] if values else None
    if raw is None:
        found = _GID_IN_FRAGMENT.search(fragment)
        raw = found.group(1) if found else None
    if raw is None:
        return None
    if not _GID.fullmatch(raw):
        raise CsvImportError(BAD_LINK)
    return raw
