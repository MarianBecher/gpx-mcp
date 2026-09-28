"""GPX metrics — geometry, elevation profile, grade, climbs. Single source of truth."""
from __future__ import annotations

from bisect import bisect_left
from math import asin, cos, radians, sin, sqrt

import gpxpy

EARTH_R = 6371008.8
GRADE_WINDOW_M = 50.0  # smoothing window for max_grade
STEEP_THRESHOLD_PCT = 5.0
PROFILE_SAMPLES = 30
GEOMETRY_SAMPLES = 20
CLIMB_BIN_M = 50.0
CLIMB_START_GRADE = 2.0   # % over a bin to open a climb
CLIMB_CONTINUE_GRADE = 0.5  # % — bins below this count as "flat" inside a climb
CLIMB_MAX_FLAT_M = 300.0  # tolerated flat/downhill stretch inside a climb
CLIMB_MIN_GAIN_M = 20.0
MAX_CLIMBS_IN_METADATA = 8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_R * asin(sqrt(a))


def parse_points(gpx_text: str) -> list[gpxpy.gpx.GPXTrackPoint]:
    parsed = gpxpy.parse(gpx_text)
    pts: list[gpxpy.gpx.GPXTrackPoint] = []
    for trk in parsed.tracks:
        for seg in trk.segments:
            pts.extend(seg.points)
    return pts


def extract_latlon(gpx_text: str) -> list[tuple[float, float]]:
    """All trackpoints as (lat, lon) — full resolution."""
    return [(p.latitude, p.longitude) for p in parse_points(gpx_text)]


def cumulative_distances(pts) -> list[float]:
    """Cumulative haversine distance (m) per trackpoint, starting at 0."""
    cum = [0.0]
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        cum.append(cum[-1] + haversine_m(a.latitude, a.longitude, b.latitude, b.longitude))
    return cum


def elevations(pts) -> list[float]:
    """Elevation per point with forward-fill for missing values (0.0 if none at all)."""
    out: list[float] = []
    last = next((p.elevation for p in pts if p.elevation is not None), 0.0)
    for p in pts:
        if p.elevation is not None:
            last = p.elevation
        out.append(float(last))
    return out


def nearest_index(pts, lat: float, lon: float) -> tuple[int, float]:
    """Index of the closest trackpoint to (lat, lon) and its distance in m."""
    best_i = 0
    best_d = float("inf")
    for i, p in enumerate(pts):
        d = haversine_m(lat, lon, p.latitude, p.longitude)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i, best_d


def index_at_distance(cum: list[float], target_m: float) -> int:
    """Index of the first trackpoint at or beyond target_m (clamped)."""
    i = bisect_left(cum, target_m)
    return min(max(i, 0), len(cum) - 1)


def interpolate_at_distance(pts, cum: list[float], elev: list[float], target_m: float) -> dict:
    """Linear interpolation of lat/lon/ele at a distance along the track."""
    target_m = max(0.0, min(target_m, cum[-1]))
    j = index_at_distance(cum, target_m)
    if j == 0 or cum[j] == cum[j - 1]:
        p = pts[j]
        return {"lat": p.latitude, "lon": p.longitude, "ele_m": round(elev[j]), "index": j}
    i = j - 1
    t = (target_m - cum[i]) / (cum[j] - cum[i])
    a, b = pts[i], pts[j]
    return {
        "lat": round(a.latitude + t * (b.latitude - a.latitude), 6),
        "lon": round(a.longitude + t * (b.longitude - a.longitude), 6),
        "ele_m": round(elev[i] + t * (elev[j] - elev[i])),
        "index": j,
    }


def _resample(cum: list[float], elev: list[float], step_m: float) -> list[tuple[float, float]]:
    """(distance_m, elevation) every step_m along the track, incl. the end."""
    total = cum[-1]
    if total <= 0:
        return [(0.0, elev[0])]
    out: list[tuple[float, float]] = []
    k = 0
    d = 0.0
    n = len(cum)
    while d < total:
        while k + 1 < n and cum[k + 1] < d:
            k += 1
        if k + 1 < n and cum[k + 1] > cum[k]:
            t = (d - cum[k]) / (cum[k + 1] - cum[k])
            out.append((d, elev[k] + t * (elev[k + 1] - elev[k])))
        else:
            out.append((d, elev[k]))
        d += step_m
    out.append((total, elev[-1]))
    return out


def windowed_max_grade(cum: list[float], elev: list[float], i0: int = 0, i1: int | None = None) -> float:
    """Steepest GRADE_WINDOW_M-windowed uphill grade (%) between point indices."""
    n = len(cum) if i1 is None else min(i1 + 1, len(cum))
    max_grade = 0.0
    j = i0
    for i in range(i0, n):
        if j < i + 1:
            j = i + 1
        while j < n and cum[j] - cum[i] < GRADE_WINDOW_M:
            j += 1
        if j >= n:
            break
        d = cum[j] - cum[i]
        if d > 0:
            g = (elev[j] - elev[i]) / d * 100
            if g > max_grade:
                max_grade = g
    return max_grade


def find_climbs(cum: list[float], elev: list[float], min_gain_m: float = CLIMB_MIN_GAIN_M) -> list[dict]:
    """Detect climbs from a resampled profile.

    A climb opens at a bin steeper than CLIMB_START_GRADE and closes after more
    than CLIMB_MAX_FLAT_M of bins below CLIMB_CONTINUE_GRADE. Returns climbs with
    gain >= min_gain_m, sorted by start.
    """
    samples = _resample(cum, elev, CLIMB_BIN_M)
    if len(samples) < 2:
        return []
    climbs: list[dict] = []
    start: int | None = None
    flat_since: int | None = None
    peak_idx = 0

    def close(s: int, e: int) -> None:
        d0, e0 = samples[s]
        d1, e1 = samples[e]
        gain = e1 - e0
        length = d1 - d0
        if gain < min_gain_m or length <= 0:
            return
        i0 = index_at_distance(cum, d0)
        i1 = index_at_distance(cum, d1)
        climbs.append(
            {
                "start_km": round(d0 / 1000, 2),
                "end_km": round(d1 / 1000, 2),
                "length_m": round(length),
                "gain_m": round(gain),
                "avg_grade_pct": round(gain / length * 100, 1),
                "max_grade_pct": round(windowed_max_grade(cum, elev, i0, i1), 1),
                "top_ele_m": round(e1),
            }
        )

    for i in range(1, len(samples)):
        d_prev, e_prev = samples[i - 1]
        d, e = samples[i]
        seg = d - d_prev
        grade = (e - e_prev) / seg * 100 if seg > 0 else 0.0
        if start is None:
            if grade >= CLIMB_START_GRADE:
                start = i - 1
                peak_idx = i
                flat_since = None
            continue
        if grade >= CLIMB_CONTINUE_GRADE:
            flat_since = None
            if e >= samples[peak_idx][1]:
                peak_idx = i
        else:
            if flat_since is None:
                flat_since = i - 1
            if d - samples[flat_since][0] > CLIMB_MAX_FLAT_M or e < samples[peak_idx][1] - 10:
                close(start, peak_idx)
                start = None
                flat_since = None
                if grade >= CLIMB_START_GRADE:
                    start = i - 1
                    peak_idx = i
    if start is not None:
        close(start, peak_idx)
    return climbs


def section_stats(cum: list[float], elev: list[float], i0: int, i1: int) -> dict:
    """Distance / ascent / descent / max grade between two point indices."""
    ascent = descent = steep = 0.0
    for i in range(i0 + 1, i1 + 1):
        de = elev[i] - elev[i - 1]
        d = cum[i] - cum[i - 1]
        if de > 0:
            ascent += de
        else:
            descent -= de
        if d >= 1.0 and de / d * 100 > STEEP_THRESHOLD_PCT:
            steep += d
    return {
        "distance_km": round((cum[i1] - cum[i0]) / 1000, 2),
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "max_grade_percent": round(windowed_max_grade(cum, elev, i0, i1), 1),
        "steep_uphill_m": round(steep),
    }


def compute_metrics(gpx_text: str) -> dict:
    """Agent-friendly summary of a GPX track. Compact arrays, no raw GPX."""
    pts = parse_points(gpx_text)
    if len(pts) < 2:
        return {
            "distance_km": 0.0, "ascent_m": 0, "descent_m": 0, "n_points": len(pts),
            "max_grade_percent": 0.0, "steep_uphill_m": 0, "min_ele_m": None, "max_ele_m": None,
            "climbs": [], "elevation_profile": [], "sampled_geometry": [], "bbox": None,
        }
    cum = cumulative_distances(pts)
    elev = elevations(pts)
    total_m = cum[-1]
    stats = section_stats(cum, elev, 0, len(pts) - 1)

    profile: list[list[float]] = []
    geometry: list[list[float]] = []
    if total_m > 0:
        for s in range(PROFILE_SAMPLES):
            target = s / (PROFILE_SAMPLES - 1) * total_m
            p = interpolate_at_distance(pts, cum, elev, target)
            profile.append([round(target / 1000, 1), p["ele_m"]])
        for s in range(GEOMETRY_SAMPLES):
            target = s / (GEOMETRY_SAMPLES - 1) * total_m
            p = pts[index_at_distance(cum, target)]
            geometry.append([round(p.latitude, 5), round(p.longitude, 5)])

    climbs = find_climbs(cum, elev)
    biggest = sorted(climbs, key=lambda c: c["gain_m"], reverse=True)[:MAX_CLIMBS_IN_METADATA]
    biggest.sort(key=lambda c: c["start_km"])

    lats = [p.latitude for p in pts]
    lons = [p.longitude for p in pts]
    return {
        **stats,
        "n_points": len(pts),
        "min_ele_m": round(min(elev)),
        "max_ele_m": round(max(elev)),
        "n_climbs": len(climbs),
        "climbs": biggest,
        "elevation_profile": profile,
        "sampled_geometry": geometry,
        "bbox": {
            "min_lat": round(min(lats), 5), "min_lon": round(min(lons), 5),
            "max_lat": round(max(lats), 5), "max_lon": round(max(lons), 5),
        },
    }
