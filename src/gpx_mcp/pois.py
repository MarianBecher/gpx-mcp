"""POI lookup via Overpass API."""
from __future__ import annotations

from typing import Literal

from .http import client

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

Category = Literal[
    "forest",
    "viewpoint",
    "cafe",
    "lake",
    "peak",
    "castle",
    "rest_area",
    "drinking_water",
]

_QUERIES: dict[str, str] = {
    "forest": '(way["landuse"="forest"](around:{r},{lat},{lon}); way["natural"="wood"](around:{r},{lat},{lon}););',
    "viewpoint": '(node["tourism"="viewpoint"](around:{r},{lat},{lon}););',
    "cafe": '(node["amenity"="cafe"](around:{r},{lat},{lon}); node["amenity"="ice_cream"](around:{r},{lat},{lon}););',
    "lake": '(way["natural"="water"]["water"="lake"](around:{r},{lat},{lon}); relation["natural"="water"]["water"="lake"](around:{r},{lat},{lon}););',
    "peak": '(node["natural"="peak"](around:{r},{lat},{lon}););',
    "castle": '(node["historic"="castle"](around:{r},{lat},{lon}); way["historic"="castle"](around:{r},{lat},{lon}););',
    "rest_area": '(node["amenity"="bench"](around:{r},{lat},{lon}); node["amenity"="shelter"](around:{r},{lat},{lon}););',
    "drinking_water": '(node["amenity"="drinking_water"](around:{r},{lat},{lon}););',
}


async def find_pois(
    lat: float,
    lon: float,
    category: Category,
    radius_m: int = 5000,
    limit: int = 30,
) -> list[dict]:
    """Find POIs of a category around a coordinate.

    Args:
        lat, lon: center point.
        category: see Category literal.
        radius_m: search radius in meters (max 50000 enforced).
        limit: max POIs returned, ranked by distance from center.
    """
    if category not in _QUERIES:
        raise ValueError(f"Unknown category: {category}. Choose from {list(_QUERIES)}.")
    r = max(100, min(radius_m, 50000))
    body = _QUERIES[category].format(r=r, lat=lat, lon=lon)
    overpass_ql = f"[out:json][timeout:25];{body}out center {limit};"

    resp = await client().post(OVERPASS_URL, data={"data": overpass_ql})
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            plat, plon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            plat, plon = center.get("lat"), center.get("lon")
        if plat is None or plon is None:
            continue
        tags = el.get("tags", {})
        results.append(
            {
                "lat": plat,
                "lon": plon,
                "name": tags.get("name") or tags.get("ref") or category,
                "tags": {k: v for k, v in tags.items() if k in {"name", "ele", "natural", "amenity", "tourism", "historic", "landuse"}},
            }
        )
    return results[:limit]
