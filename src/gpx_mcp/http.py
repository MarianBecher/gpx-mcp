"""Shared HTTP client. OSM-based services require an identifying User-Agent."""
from __future__ import annotations

import httpx

USER_AGENT = "gpx-mcp/0.1 (https://github.com/local/gpx-mcp; bike route planning agent)"

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
