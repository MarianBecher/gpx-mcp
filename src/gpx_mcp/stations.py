"""Train station lookup via Overpass, around a point or along a cached route.

Each station gets a `likely_bike_friendly` heuristic from OSM tags (regional
halts and S-Bahn stations usually allow bikes; ICE needs a reservation, which
OSM does not encode).
"""
from __future__ import annotations

from . import state
from .metrics import haversine_m, nearest_index
from .overpass import around_clause, query
from .pois import sample_track

_STATION_FILTER = (
    'node["railway"~"^(station|halt)$"]'
    '["disused"!~"."]["abandoned"!~"."]["construction"!~"."]'
)
_KEEP_TAGS = {
    "name", "railway", "operator", "network", "ref", "uic_ref",
    "train", "light_rail", "subway", "tram", "station", "wheelchair",
}
_BIKE_FRIENDLY_NETWORK_HINTS = (
    "s-bahn", "rb", "re ", "vgn", "vvs", "mvv", "rmv", "vrr", "vbb",
    "vbn", "naldo", "kvv", "vrn", "regiobahn", "agilis", "brb",
)


def _classify_bike_friendly(tags: dict) -> bool:
    station = tags.get("station")
    if station == "subway" and tags.get("train") != "yes":
        return False
    if tags.get("train") == "yes" or "uic_ref" in tags or tags.get("railway") == "halt":
        return True
    if tags.get("light_rail") == "yes" or station == "light_rail":
        return True
    blob = f"{tags.get('network', '')} {tags.get('operator', '')}".lower()
    return any(h in blob for h in _BIKE_FRIENDLY_NETWORK_HINTS)


def _station(el: dict) -> dict | None:
    if el.get("type") != "node" or el.get("lat") is None:
        return None
    tags = el.get("tags", {})
    return {
        "lat": el["lat"],
        "lon": el["lon"],
        "name": tags.get("name") or tags.get("uic_name") or "?",
        "type": tags.get("railway", "station"),
        "operator": tags.get("operator"),
        "network": tags.get("network"),
        "likely_bike_friendly": _classify_bike_friendly(tags),
        "tags": {k: v for k, v in tags.items() if k in _KEEP_TAGS and k not in ("name", "operator", "network")},
    }


def _dedupe(stations: list[dict]) -> list[dict]:
    """Collapse same-name nodes within 500 m (DB platform + subway entrance etc.)."""
    out: list[dict] = []
    for s in stations:
        match = None
        for e in out:
            if e["name"] == s["name"] != "?" and haversine_m(e["lat"], e["lon"], s["lat"], s["lon"]) < 500:
                match = e
                break
        if match is None:
            out.append(s)
        elif s["likely_bike_friendly"] and not match["likely_bike_friendly"]:
            out[out.index(match)] = s
    return out


async def find_stations_around(lat: float, lon: float, radius_m: int = 10000, limit: int = 30) -> list[dict]:
    r = max(200, min(radius_m, 50000))
    data = await query(f"({_STATION_FILTER}(around:{r},{lat},{lon}););out body {limit * 2};")
    out = []
    for el in data.get("elements", []):
        s = _station(el)
        if s:
            s["distance_m"] = round(haversine_m(lat, lon, s["lat"], s["lon"]))
            out.append(s)
    out.sort(key=lambda s: s["distance_m"])
    return _dedupe(out)[:limit]


async def find_stations_along_route(route_id: str, max_detour_m: int = 3000, limit: int = 30) -> list[dict]:
    detour = max(100, min(max_detour_m, 20000))
    cached = state.get(route_id)
    pts, cum = cached.pts, cached.cum
    if len(pts) < 2:
        return []
    samples = sample_track(cached, max(500.0, detour * 0.8))
    data = await query(f"({_STATION_FILTER}{around_clause(detour, samples)};);out body;", timeout_s=40)
    out = []
    for el in data.get("elements", []):
        s = _station(el)
        if not s:
            continue
        idx, d = nearest_index(pts, s["lat"], s["lon"])
        if d > detour:
            continue
        s["km_position"] = round(cum[idx] / 1000, 2)
        s["detour_m"] = round(d)
        out.append(s)
    out.sort(key=lambda s: s["km_position"])
    return _dedupe(out)[:limit]
