"""Train station lookup via Overpass.

Two modes: around a point or along a cached route. Each station gets a
`likely_bike_friendly` heuristic derived from OSM tags (regional halts and
S-Bahn stations usually allow bikes off-peak; ICE-only main stations do too
but with reservation requirements not encoded in OSM).
"""
from __future__ import annotations

from . import state
from .http import client
from .metrics import cumulative_distances, haversine_m, nearest_index, parse_points
from .pois import OVERPASS_URL

# Filter out former/planned stations. Overpass needs the negation via regex match.
_STATION_FILTER = (
    'node["railway"~"^(station|halt)$"]'
    '["disused"!~"."]'
    '["abandoned"!~"."]'
    '["construction"!~"."]'
)

_KEEP_TAGS = {
    "name", "railway", "operator", "network", "ref", "uic_name", "uic_ref",
    "train", "light_rail", "subway", "tram", "station", "wheelchair",
}

# Heuristic: tags that strongly hint at regional / S-Bahn / RB-RE access,
# where bike transport is generally allowed (often free off-peak in DE).
_BIKE_FRIENDLY_NETWORK_HINTS = (
    "s-bahn", "rb", "re ", "vgn", "vvs", "mvv", "rmv", "vrr", "vbb",
    "vbn", "naldo", "kvv", "vrn", "regiobahn", "agilis", "brb",
)


def _classify_bike_friendly(tags: dict) -> bool:
    """Soft signal — OSM doesn't reliably tag train-type bike rules.

    Order matters: explicit subway-only nodes are *not* bike-friendly even if
    they share an operator field with regional rail.
    """
    station = tags.get("station")
    if station == "subway" and tags.get("train") != "yes":
        return False
    if tags.get("train") == "yes":
        return True
    if "uic_ref" in tags:
        return True
    if tags.get("railway") == "halt":
        return True
    if tags.get("light_rail") == "yes" or station == "light_rail":
        return True
    network = (tags.get("network") or "").lower()
    operator = (tags.get("operator") or "").lower()
    blob = f"{network} {operator}"
    return any(hint in blob for hint in _BIKE_FRIENDLY_NETWORK_HINTS)


def _station_dict(el: dict) -> dict | None:
    if el.get("type") != "node":
        return None
    lat, lon = el.get("lat"), el.get("lon")
    if lat is None or lon is None:
        return None
    tags = el.get("tags", {})
    name = tags.get("name") or tags.get("uic_name") or "?"
    return {
        "lat": lat,
        "lon": lon,
        "name": name,
        "type": tags.get("railway", "station"),
        "operator": tags.get("operator"),
        "network": tags.get("network"),
        "uic_ref": tags.get("uic_ref"),
        "likely_bike_friendly": _classify_bike_friendly(tags),
        "tags": {k: v for k, v in tags.items() if k in _KEEP_TAGS},
    }


def _dedupe_by_name(stations: list[dict]) -> list[dict]:
    """Collapse same-name nodes that sit within 500 m of each other.

    OSM frequently has separate nodes for the DB platform and the subway
    entrance of a main station (different operator tags, same Name). We
    keep the bike-friendly variant if any.
    """
    out: list[dict] = []
    for s in stations:
        match = None
        for existing in out:
            if existing["name"] == s["name"] and existing["name"] != "?":
                if haversine_m(existing["lat"], existing["lon"], s["lat"], s["lon"]) < 500:
                    match = existing
                    break
        if match is None:
            out.append(s)
            continue
        if s["likely_bike_friendly"] and not match["likely_bike_friendly"]:
            out[out.index(match)] = s
    return out


async def find_stations_around(
    lat: float,
    lon: float,
    radius_m: int = 10000,
    limit: int = 30,
) -> list[dict]:
    """Find train stations and halts around a coordinate, sorted by distance."""
    r = max(200, min(radius_m, 50000))
    body = f"({_STATION_FILTER}(around:{r},{lat},{lon}););"
    # Fetch more than `limit` because dedupe may collapse pairs.
    overpass_ql = f"[out:json][timeout:30];{body}out body {limit * 2};"

    resp = await client().post(OVERPASS_URL, data={"data": overpass_ql})
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for el in data.get("elements", []):
        s = _station_dict(el)
        if s is None:
            continue
        s["distance_m"] = round(haversine_m(lat, lon, s["lat"], s["lon"]), 0)
        results.append(s)

    results.sort(key=lambda s: s["distance_m"])
    return _dedupe_by_name(results)[:limit]


async def find_stations_along_route(
    route_id: str,
    max_detour_m: int = 3000,
    limit: int = 30,
) -> list[dict]:
    """Find stations within `max_detour_m` of a cached route, sorted by km_position.

    Use for one-way tours: pick a start near one and a finish near another, or
    identify abort points along a longer route.
    """
    detour = max(100, min(max_detour_m, 20000))

    cached = state.get(route_id)
    pts = parse_points(cached.gpx)
    if len(pts) < 2:
        return []
    cum = cumulative_distances(pts)

    # Stations are sparser than POIs; sample more coarsely to keep URL small.
    stride_m = max(500.0, detour * 0.8)
    samples: list[tuple[float, float]] = [(pts[0].latitude, pts[0].longitude)]
    target = stride_m
    for i in range(1, len(pts)):
        if cum[i] >= target:
            samples.append((pts[i].latitude, pts[i].longitude))
            while target <= cum[i]:
                target += stride_m
    last = pts[-1]
    if samples[-1] != (last.latitude, last.longitude):
        samples.append((last.latitude, last.longitude))

    around_coords = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in samples)
    body = f"({_STATION_FILTER}(around:{detour},{around_coords}););"
    overpass_ql = f"[out:json][timeout:60];{body}out body;"

    resp = await client().post(OVERPASS_URL, data={"data": overpass_ql})
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for el in data.get("elements", []):
        s = _station_dict(el)
        if s is None:
            continue
        idx, dist_m = nearest_index(pts, s["lat"], s["lon"])
        if dist_m > detour:
            continue
        s["km_position"] = round(cum[idx] / 1000, 2)
        s["detour_m"] = round(dist_m, 0)
        results.append(s)

    results.sort(key=lambda s: s["km_position"])
    return _dedupe_by_name(results)[:limit]
