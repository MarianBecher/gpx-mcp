"""Heuristic detour suggestion to converge on a target route length faster."""
from __future__ import annotations

from math import atan2, cos, degrees, radians, sin

from .metrics import haversine_m
from .pois import Category, find_pois


def _midpoint(waypoints: list[tuple[float, float]]) -> tuple[float, float]:
    """Pick the waypoint or midpoint at the geometric center along the route."""
    if len(waypoints) == 2:
        a, b = waypoints[0], waypoints[1]
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    mid_idx = len(waypoints) // 2
    return waypoints[mid_idx]


def _offset_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Move (lat, lon) by distance_m along bearing_deg. Simple spherical model."""
    r = 6371008.8
    br = radians(bearing_deg)
    lat1 = radians(lat)
    lon1 = radians(lon)
    d = distance_m / r
    lat2 = sin(lat1) * cos(d) + cos(lat1) * sin(d) * cos(br)
    from math import asin
    lat2 = asin(lat2)
    lon2 = lon1 + atan2(sin(br) * sin(d) * cos(lat1), cos(d) - sin(lat1) * sin(lat2))
    return (degrees(lat2), degrees(lon2))


async def suggest_detour(
    current_waypoints: list[tuple[float, float]],
    target_extra_km: float,
    poi_category: Category = "viewpoint",
) -> dict:
    """Suggest one extra waypoint to insert that should add ~target_extra_km.

    Approach: from the midpoint, offset perpendicular to the start–end line by
    half the desired extra distance, then snap to the nearest POI of the
    requested category. Routing through it should add roughly target_extra_km
    (geometry is approximate — agent should re-route and measure).
    """
    if len(current_waypoints) < 2:
        raise ValueError("Need at least 2 current waypoints.")
    if target_extra_km <= 0:
        raise ValueError("target_extra_km must be positive — use a shorter direct route instead.")

    start = current_waypoints[0]
    end = current_waypoints[-1]
    mid = _midpoint(current_waypoints)

    bearing = atan2(
        sin(radians(end[1] - start[1])) * cos(radians(end[0])),
        cos(radians(start[0])) * sin(radians(end[0]))
        - sin(radians(start[0])) * cos(radians(end[0])) * cos(radians(end[1] - start[1])),
    )
    perp_deg = (degrees(bearing) + 90) % 360

    offset_m = (target_extra_km * 1000) / 2
    candidate_lat, candidate_lon = _offset_point(mid[0], mid[1], perp_deg, offset_m)

    search_radius = max(2000, int(offset_m * 0.6))
    pois = await find_pois(candidate_lat, candidate_lon, poi_category, radius_m=search_radius, limit=10)

    if not pois:
        return {
            "candidate": {"lat": candidate_lat, "lon": candidate_lon, "name": "synthetic-offset"},
            "alternatives": [],
            "note": f"No POI of type '{poi_category}' near offset point — try another category or use the synthetic candidate directly.",
        }

    for p in pois:
        p["distance_to_candidate_m"] = round(
            haversine_m(candidate_lat, candidate_lon, p["lat"], p["lon"]), 0
        )
    pois.sort(key=lambda p: p["distance_to_candidate_m"])
    best = pois[0]
    return {
        "candidate": {"lat": best["lat"], "lon": best["lon"], "name": best["name"]},
        "alternatives": pois[1:5],
        "note": f"Insert candidate between waypoint {len(current_waypoints)//2 - 1} and {len(current_waypoints)//2}, then re-route and verify length.",
    }
