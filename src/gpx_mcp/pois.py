"""POI lookup via Overpass API.

Two query modes:
- `find_pois`: around a single (lat, lon) center.
- `find_pois_along_route`: along a cached route_id, with detour-distance filtering.
"""
from __future__ import annotations

from typing import Literal

from . import state
from .http import client
from .metrics import cumulative_distances, nearest_index, parse_points

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

# Tag selectors per category, without the around clause — we paste it in.
_TAG_PATTERNS: dict[str, list[str]] = {
    "forest": ['way["landuse"="forest"]', 'way["natural"="wood"]'],
    "viewpoint": ['node["tourism"="viewpoint"]'],
    "cafe": ['node["amenity"="cafe"]', 'node["amenity"="ice_cream"]'],
    "lake": [
        'way["natural"="water"]["water"="lake"]',
        'relation["natural"="water"]["water"="lake"]',
    ],
    "peak": ['node["natural"="peak"]'],
    "castle": ['node["historic"="castle"]', 'way["historic"="castle"]'],
    "rest_area": ['node["amenity"="bench"]', 'node["amenity"="shelter"]'],
    "drinking_water": ['node["amenity"="drinking_water"]'],
}

_KEEP_TAGS = {"name", "ele", "natural", "amenity", "tourism", "historic", "landuse"}
# Approx track-sampling stride for along-route queries — keeps URL bounded while
# still covering every track meter (each sample contributes a circle of radius
# max_detour_m, and ~250 m between samples keeps gaps to zero up to a 300 m radius).
_ALONG_SAMPLE_STRIDE_M = 250.0


def _build_query(filter_clause: str, category: Category, limit: int | None) -> str:
    selectors = _TAG_PATTERNS[category]
    body = "".join(f"{sel}{filter_clause};" for sel in selectors)
    limit_clause = f" {limit}" if limit else ""
    return f"[out:json][timeout:60];({body});out center{limit_clause};"


def _extract_point(el: dict) -> tuple[float | None, float | None]:
    if el["type"] == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def _poi_dict(el: dict, plat: float, plon: float, category: Category) -> dict:
    tags = el.get("tags", {})
    return {
        "lat": plat,
        "lon": plon,
        "name": tags.get("name") or tags.get("ref") or category,
        "tags": {k: v for k, v in tags.items() if k in _KEEP_TAGS},
    }


async def find_pois(
    lat: float,
    lon: float,
    category: Category,
    radius_m: int = 5000,
    limit: int = 30,
) -> list[dict]:
    """Find POIs of a category around a coordinate."""
    if category not in _TAG_PATTERNS:
        raise ValueError(f"Unknown category: {category}. Choose from {list(_TAG_PATTERNS)}.")
    r = max(100, min(radius_m, 50000))
    filter_clause = f"(around:{r},{lat},{lon})"
    overpass_ql = _build_query(filter_clause, category, limit)

    resp = await client().post(OVERPASS_URL, data={"data": overpass_ql})
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for el in data.get("elements", []):
        plat, plon = _extract_point(el)
        if plat is None or plon is None:
            continue
        results.append(_poi_dict(el, plat, plon, category))
    return results[:limit]


def _sample_along_track(pts, cum: list[float], stride_m: float) -> list[tuple[float, float]]:
    """Pick trackpoints at ~stride_m intervals plus the last one. Returns (lat, lon)."""
    if not pts:
        return []
    samples = [(pts[0].latitude, pts[0].longitude)]
    target = stride_m
    total = cum[-1]
    for i in range(1, len(pts)):
        if cum[i] >= target:
            samples.append((pts[i].latitude, pts[i].longitude))
            while target <= cum[i]:
                target += stride_m
    last = pts[-1]
    if samples[-1] != (last.latitude, last.longitude) and total > 0:
        samples.append((last.latitude, last.longitude))
    return samples


async def find_pois_along_route(
    route_id: str,
    category: Category,
    max_detour_m: int = 1000,
    limit: int = 50,
) -> list[dict]:
    """Find POIs of a category within `max_detour_m` of a cached route.

    Args:
        route_id: handle from `route` or `load_gpx`.
        category: same set as `find_pois`.
        max_detour_m: max perpendicular distance from the track (10..5000).
        limit: max POIs returned, sorted by km_position along the track.

    Each result carries `km_position` (where on the route the POI sits) and
    `detour_m` (haversine distance to the closest trackpoint).
    """
    if category not in _TAG_PATTERNS:
        raise ValueError(f"Unknown category: {category}. Choose from {list(_TAG_PATTERNS)}.")
    detour = max(10, min(max_detour_m, 5000))

    cached = state.get(route_id)
    pts = parse_points(cached.gpx)
    if len(pts) < 2:
        return []
    cum = cumulative_distances(pts)

    samples = _sample_along_track(pts, cum, _ALONG_SAMPLE_STRIDE_M)
    around_coords = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in samples)
    filter_clause = f"(around:{detour},{around_coords})"
    overpass_ql = _build_query(filter_clause, category, limit=None)

    resp = await client().post(OVERPASS_URL, data={"data": overpass_ql})
    resp.raise_for_status()
    data = resp.json()

    seen: set[tuple[float, float]] = set()
    results: list[dict] = []
    for el in data.get("elements", []):
        plat, plon = _extract_point(el)
        if plat is None or plon is None:
            continue
        key = (round(plat, 5), round(plon, 5))
        if key in seen:
            continue
        seen.add(key)
        idx, dist_m = nearest_index(pts, plat, plon)
        if dist_m > detour:
            continue
        poi = _poi_dict(el, plat, plon, category)
        poi["km_position"] = round(cum[idx] / 1000, 2)
        poi["detour_m"] = round(dist_m, 0)
        results.append(poi)

    results.sort(key=lambda p: p["km_position"])
    return results[:limit]
