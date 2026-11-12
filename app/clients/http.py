"""Shared httpx client factory: one timeout policy and one User-Agent for every outgoing call."""

import httpx

from app import __version__

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
REQUEST_TIMEOUT = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
USER_AGENT = f"osu-tourney-hub/{__version__} (+https://github.com/NiklaskoDev/osu-tourney-hub)"


def make_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Build the client. Tests pass a `httpx.MockTransport`; production passes nothing."""
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        transport=transport,
        follow_redirects=False,
    )
