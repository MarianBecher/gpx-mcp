"""Geocoding via Nominatim. Fair-use: 1 req/s, identifying User-Agent required."""
from __future__ import annotations

import asyncio

from .http import client

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
_rate_limit_lock = asyncio.Lock()
_last_call_ts: float = 0.0
_MIN_INTERVAL_S = 1.05

_PLACE_KEYS = ("village", "hamlet", "suburb", "town", "city", "municipality", "county")


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
    """Resolve a place name to coordinates."""
    await _throttle()
    params: dict[str, str | int] = {
        "q": query,
        "format": "jsonv2",
        "limit": max(1, min(limit, 10)),
        "addressdetails": 0,
    }
    if country_codes:
        params["countrycodes"] = country_codes
    resp = await client().get(NOMINATIM_SEARCH, params=params)
    resp.raise_for_status()
    return [
        {
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "display_name": r["display_name"],
            "type": r.get("type", ""),
        }
        for r in resp.json()
    ]


async def reverse(lat: float, lon: float, zoom: int = 16) -> dict:
    """Coordinates → nearest address. zoom 16 ≈ street, 14 ≈ suburb, 10 ≈ city."""
    await _throttle()
    params: dict[str, str | int | float] = {
        "lat": lat, "lon": lon, "format": "jsonv2", "zoom": max(3, min(zoom, 18)), "addressdetails": 1,
    }
    resp = await client().get(NOMINATIM_REVERSE, params=params)
    resp.raise_for_status()
    r = resp.json()
    addr = r.get("address", {})
    place = next((addr[k] for k in _PLACE_KEYS if k in addr), None)
    return {
        "lat": lat,
        "lon": lon,
        "display_name": r.get("display_name"),
        "road": addr.get("road") or addr.get("pedestrian") or addr.get("cycleway") or addr.get("path"),
        "place": place,
        "postcode": addr.get("postcode"),
        "state": addr.get("state"),
        "type": r.get("type"),
    }
