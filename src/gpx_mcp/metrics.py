"""GPX metrics — geometry, elevation profile, grade. Single source of truth."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

import gpxpy

EARTH_R = 6371008.8
GRADE_WINDOW_M = 50.0  # smoothing window for max_grade
STEEP_THRESHOLD_PCT = 5.0
PROFILE_SAMPLES = 30
GEOMETRY_SAMPLES = 20  # sampled lat/lon for agent reasoning


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_R * asin(sqrt(a))


def _all_points(gpx_text: str) -> list[gpxpy.gpx.GPXTrackPoint]:
    parsed = gpxpy.parse(gpx_text)
    pts: list[gpxpy.gpx.GPXTrackPoint] = []
    for trk in parsed.tracks:
        for seg in trk.segments:
            pts.extend(seg.points)
    return pts


def extract_latlon(gpx_text: str) -> list[tuple[float, float]]:
    """Return all trackpoints as (lat, lon) tuples — full resolution, no sampling."""
    return [(p.latitude, p.longitude) for p in _all_points(gpx_text)]


def parse_points(gpx_text: str) -> list[gpxpy.gpx.GPXTrackPoint]:
    """Public alias of the internal point extractor."""
    return _all_points(gpx_text)


def cumulative_distances(pts: list[gpxpy.gpx.GPXTrackPoint]) -> list[float]:
    """Cumulative haversine distance (m) per trackpoint, starting at 0."""
    cum = [0.0]
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        cum.append(cum[-1] + haversine_m(a.latitude, a.longitude, b.latitude, b.longitude))
    return cum


def nearest_index(
    pts: list[gpxpy.gpx.GPXTrackPoint], lat: float, lon: float
) -> tuple[int, float]:
    """Index of the closest trackpoint to (lat, lon) and its distance in m.

    Per-point haversine, no segment projection — for BRouter tracks (~30 m spacing)
    the approximation is < 15 m, far below typical detour tolerances.
    """
    best_i = 0
    best_d = float("inf")
    for i, p in enumerate(pts):
        d = haversine_m(lat, lon, p.latitude, p.longitude)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i, best_d


def compute_metrics(gpx_text: str) -> dict:
    """Compute full metric set for a GPX track. Agent-friendly summary, no raw GPX."""
    pts = _all_points(gpx_text)
    if len(pts) < 2:
        return {
            "distance_km": 0.0,
            "ascent_m": 0,
            "descent_m": 0,
            "n_points": len(pts),
            "max_grade_percent": 0.0,
            "steep_uphill_m": 0,
            "elevation_profile": [],
            "sampled_geometry": [],
            "bbox": None,
        }

    cum_d: list[float] = [0.0]
    elev: list[float] = []
    e0 = pts[0].elevation if pts[0].elevation is not None else 0.0
    elev.append(e0)
    last_known = e0
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        d = haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
        cum_d.append(cum_d[-1] + d)
        if b.elevation is not None:
            last_known = b.elevation
        elev.append(last_known)

    total_m = cum_d[-1]

    ascent = 0.0
    descent = 0.0
    steep_uphill_m = 0.0
    for i in range(1, len(pts)):
        de = elev[i] - elev[i - 1]
        d = cum_d[i] - cum_d[i - 1]
        if de > 0:
            ascent += de
        else:
            descent -= de
        if d >= 1.0 and (de / d) * 100 > STEEP_THRESHOLD_PCT:
            steep_uphill_m += d

    max_grade = 0.0
    j = 0
    n = len(pts)
    for i in range(n):
        if j < i + 1:
            j = i + 1
        while j < n and cum_d[j] - cum_d[i] < GRADE_WINDOW_M:
            j += 1
        if j >= n:
            break
        d = cum_d[j] - cum_d[i]
        de = elev[j] - elev[i]
        if d > 0:
            g = (de / d) * 100
            if g > max_grade:
                max_grade = g

    profile: list[dict] = []
    if total_m > 0:
        k = 0
        for s in range(PROFILE_SAMPLES):
            target = (s / (PROFILE_SAMPLES - 1)) * total_m
            while k + 1 < n and cum_d[k + 1] < target:
                k += 1
            if k + 1 < n and cum_d[k + 1] > cum_d[k]:
                t = (target - cum_d[k]) / (cum_d[k + 1] - cum_d[k])
                e = elev[k] + t * (elev[k + 1] - elev[k])
            else:
                e = elev[k]
            profile.append({"km": round(target / 1000, 2), "elev_m": round(e, 0)})

    geometry: list[dict] = []
    if total_m > 0:
        k = 0
        for s in range(GEOMETRY_SAMPLES):
            target = (s / (GEOMETRY_SAMPLES - 1)) * total_m
            while k + 1 < n and cum_d[k + 1] < target:
                k += 1
            p = pts[k]
            geometry.append({"lat": round(p.latitude, 5), "lon": round(p.longitude, 5)})

    lats = [p.latitude for p in pts]
    lons = [p.longitude for p in pts]

    return {
        "distance_km": round(total_m / 1000, 2),
        "ascent_m": round(ascent, 0),
        "descent_m": round(descent, 0),
        "n_points": n,
        "max_grade_percent": round(max_grade, 1),
        "steep_uphill_m": round(steep_uphill_m, 0),
        "elevation_profile": profile,
        "sampled_geometry": geometry,
        "bbox": {
            "min_lat": min(lats),
            "min_lon": min(lons),
            "max_lat": max(lats),
            "max_lon": max(lons),
        },
    }
