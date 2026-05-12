"""Geocoding via Nominatim. Fair-use: 1 req/s, identifying User-Agent required."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .http import client

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_rate_limit_lock = asyncio.Lock()
_last_call_ts: float = 0.0
_MIN_INTERVAL_S = 1.05


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    display_name: str
    type: str

    def as_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "display_name": self.display_name,
            "type": self.type,
        }


async def _throttle() -> None:
    global _last_call_ts
    async with _rate_limit_lock:
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = _MIN_INTERVAL_S - (now - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = loop.time()


async def geocode(query: str, limit: int = 3, country_codes: str | None = "de") -> list[dict]:
    """Resolve a place name to coordinates.

    Args:
        query: free-text place name, e.g. "Nürnberg" or "Feucht, Bayern".
        limit: max results (1–10).
        country_codes: ISO 3166-1 alpha-2 comma list to restrict; None = worldwide.
    """
    await _throttle()
    params: dict[str, str | int] = {
        "q": query,
        "format": "jsonv2",
        "limit": max(1, min(limit, 10)),
        "addressdetails": 0,
    }
    if country_codes:
        params["countrycodes"] = country_codes

    resp = await client().get(NOMINATIM_URL, params=params)
    resp.raise_for_status()
    raw = resp.json()
    return [
        GeocodeResult(
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            display_name=r["display_name"],
            type=r.get("type", ""),
        ).as_dict()
        for r in raw
    ]
