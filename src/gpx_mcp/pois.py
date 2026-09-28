"""POI lookup via Overpass: around a point, along a cached route, plus signed cycle routes."""
from __future__ import annotations

import re
from typing import Literal

from . import state
from .metrics import haversine_m, index_at_distance, nearest_index
from .overpass import around_clause, element_point, query

Category = Literal[
    "forest", "viewpoint", "cafe", "bakery", "restaurant", "biergarten", "supermarket",
    "lake", "swimming", "peak", "castle", "rest_area", "shelter", "picnic", "drinking_water",
    "toilets", "bike_shop", "bike_repair", "charging_station", "campsite", "accommodation",
    "fuel", "ferry", "attraction",
]

# Tag selectors per category (without the spatial clause).
CATEGORIES: dict[str, list[str]] = {
    "forest": ['way["landuse"="forest"]', 'way["natural"="wood"]'],
    "viewpoint": ['node["tourism"="viewpoint"]'],
    "cafe": ['nwr["amenity"="cafe"]', 'nwr["amenity"="ice_cream"]'],
    "bakery": ['nwr["shop"="bakery"]'],
    "restaurant": ['nwr["amenity"="restaurant"]', 'nwr["amenity"="biergarten"]', 'nwr["amenity"="fast_food"]'],
    "biergarten": ['nwr["amenity"="biergarten"]', 'nwr["biergarten"="yes"]'],
    "supermarket": ['nwr["shop"="supermarket"]', 'nwr["shop"="convenience"]'],
    "lake": ['way["natural"="water"]["water"~"^(lake|reservoir|pond)$"]', 'relation["natural"="water"]["water"~"^(lake|reservoir)$"]'],
    "swimming": ['nwr["leisure"="swimming_area"]', 'nwr["natural"="beach"]', 'nwr["leisure"="bathing_place"]', 'nwr["leisure"="water_park"]'],
    "peak": ['node["natural"="peak"]'],
    "castle": ['nwr["historic"="castle"]', 'nwr["historic"="ruins"]["ruins"="castle"]'],
    "rest_area": ['node["amenity"="bench"]', 'node["amenity"="shelter"]', 'nwr["tourism"="picnic_site"]'],
    "shelter": ['nwr["amenity"="shelter"]'],
    "picnic": ['nwr["tourism"="picnic_site"]', 'node["leisure"="picnic_table"]'],
    "drinking_water": ['node["amenity"="drinking_water"]', 'node["drinking_water"="yes"]'],
    "toilets": ['nwr["amenity"="toilets"]'],
    "bike_shop": ['nwr["shop"="bicycle"]'],
    "bike_repair": ['node["amenity"="bicycle_repair_station"]', 'nwr["service:bicycle:repair"="yes"]'],
    "charging_station": ['nwr["amenity"="charging_station"]["bicycle"="yes"]'],
    "campsite": ['nwr["tourism"="camp_site"]'],
    "accommodation": ['nwr["tourism"~"^(hotel|guest_house|hostel|alpine_hut|chalet|motel)$"]'],
    "fuel": ['nwr["amenity"="fuel"]'],
    "ferry": ['nwr["amenity"="ferry_terminal"]'],
    "attraction": ['nwr["tourism"="attraction"]', 'nwr["historic"~"^(monument|memorial|city_gate|tower)$"]'],
}

_KEEP_TAGS = {
    "name", "ele", "natural", "amenity", "tourism", "historic", "landuse", "shop", "leisure",
    "opening_hours", "website", "phone", "cuisine", "outdoor_seating", "fee", "bicycle",
    "capacity", "stars", "description",
}
_CUSTOM_FILTER = re.compile(r'^(\[["\w:]+(?:[=~!]+"[^"\]]*")?\])+$')


def _selectors(category: str | None, custom_filter: str | None) -> list[str]:
    if custom_filter:
        cf = custom_filter.strip()
        if not _CUSTOM_FILTER.match(cf):
            raise ValueError(
                'custom_filter must be Overpass tag filters like [\"amenity\"=\"cafe\"][\"outdoor_seating\"=\"yes\"]'
            )
        return [f"nwr{cf}"]
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category {category!r}. Choose from {sorted(CATEGORIES)} or pass custom_filter.")
    return CATEGORIES[category]


def _build(selectors: list[str], spatial: str, limit: int | None) -> str:
    body = "".join(f"{sel}{spatial};" for sel in selectors)
    lim = f" {limit}" if limit else ""
    return f"({body});out center tags{lim};"


def _poi(el: dict, lat: float, lon: float, fallback_name: str) -> dict:
    tags = el.get("tags", {})
    return {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "name": tags.get("name") or tags.get("ref") or fallback_name,
        "osm": f"{el['type']}/{el['id']}",
        "tags": {k: v for k, v in tags.items() if k in _KEEP_TAGS and k != "name"},
    }


def sample_track(cached: state.CachedRoute, stride_m: float) -> list[tuple[float, float]]:
    """Trackpoints roughly every stride_m plus the last one — for `around` chains."""
    pts, cum = cached.pts, cached.cum
    if not pts:
        return []
    samples = [(pts[0].latitude, pts[0].longitude)]
    target = stride_m
    for i in range(1, len(pts)):
        if cum[i] >= target:
            samples.append((pts[i].latitude, pts[i].longitude))
            while target <= cum[i]:
                target += stride_m
    last = (pts[-1].latitude, pts[-1].longitude)
    if samples[-1] != last:
        samples.append(last)
    return samples


async def find_pois(
    lat: float,
    lon: float,
    category: str | None = None,
    custom_filter: str | None = None,
    radius_m: int = 5000,
    limit: int = 30,
) -> list[dict]:
    r = max(100, min(radius_m, 50000))
    ql = _build(_selectors(category, custom_filter), f"(around:{r},{lat},{lon})", limit * 2)
    data = await query(ql)
    out: list[dict] = []
    for el in data.get("elements", []):
        plat, plon = element_point(el)
        if plat is None or plon is None:
            continue
        p = _poi(el, plat, plon, category or "poi")
        p["distance_m"] = round(haversine_m(lat, lon, plat, plon))
        out.append(p)
    out.sort(key=lambda p: p["distance_m"])
    return out[:limit]


async def find_pois_along_route(
    route_id: str,
    category: str | None = None,
    custom_filter: str | None = None,
    max_detour_m: int = 1000,
    limit: int = 50,
    from_km: float | None = None,
    to_km: float | None = None,
) -> list[dict]:
    detour = max(10, min(max_detour_m, 5000))
    cached = state.get(route_id)
    pts, cum = cached.pts, cached.cum
    if len(pts) < 2:
        return []
    # Circles of radius `detour` every `stride` metres cover the track without gaps
    # as long as stride <= 2*detour; 0.8*detour keeps the union close to a true buffer.
    stride = max(200.0, min(detour * 0.8, 2000.0))
    samples = sample_track(cached, stride)
    if from_km is not None or to_km is not None:
        a = (from_km or 0.0) * 1000
        b = (to_km * 1000) if to_km is not None else cum[-1]
        i0, i1 = index_at_distance(cum, a), index_at_distance(cum, b)
        sub = [(p.latitude, p.longitude) for p in pts[i0 : i1 + 1]]
        samples = [s for s in samples if s in set(sub)] or sub[:: max(1, len(sub) // 60)]
    ql = _build(_selectors(category, custom_filter), around_clause(detour, samples), None)
    data = await query(ql, timeout_s=40)

    seen: set[str] = set()
    out: list[dict] = []
    for el in data.get("elements", []):
        plat, plon = element_point(el)
        if plat is None or plon is None:
            continue
        key = f"{el['type']}/{el['id']}"
        if key in seen:
            continue
        seen.add(key)
        idx, dist_m = nearest_index(pts, plat, plon)
        if dist_m > detour:
            continue
        p = _poi(el, plat, plon, category or "poi")
        p["km_position"] = round(cum[idx] / 1000, 2)
        p["detour_m"] = round(dist_m)
        out.append(p)
    out.sort(key=lambda p: p["km_position"])
    return out[:limit]


async def find_cycle_routes(
    lat: float | None = None,
    lon: float | None = None,
    route_id: str | None = None,
    radius_m: int = 2000,
    limit: int = 30,
) -> list[dict]:
    """Signed cycle routes (OSM route=bicycle relations) near a point or along a route."""
    if route_id is not None:
        cached = state.get(route_id)
        spatial = around_clause(radius_m, sample_track(cached, max(500.0, radius_m * 0.8)))
    elif lat is not None and lon is not None:
        spatial = f"(around:{max(100, min(radius_m, 30000))},{lat},{lon})"
    else:
        raise ValueError("Provide route_id or lat+lon.")
    ql = f'way["highway"]{spatial}->.w;rel(bw.w)["route"="bicycle"];out tags;'
    data = await query(ql, timeout_s=40)
    out = []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        out.append(
            {
                "name": t.get("name") or t.get("ref") or "?",
                "ref": t.get("ref"),
                "network": t.get("network"),
                "distance_km": t.get("distance"),
                "from": t.get("from"),
                "to": t.get("to"),
                "website": t.get("website"),
                "osm": f"relation/{el['id']}",
            }
        )
    order = {"icn": 0, "ncn": 1, "rcn": 2, "lcn": 3}
    out.sort(key=lambda r: (order.get(r["network"] or "", 9), r["name"]))
    return out[:limit]
