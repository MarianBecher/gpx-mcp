"""Surface classification using BRouter CSV output (per-segment way tags)."""
from __future__ import annotations

from typing import Literal

from .http import client
from .metrics import haversine_m

BROUTER_URL = "https://brouter.de/brouter"

Klass = Literal["cycleway", "road", "track", "path", "unknown"]

ALL_KLASSES: tuple[Klass, ...] = ("cycleway", "road", "track", "path", "unknown")

_ROAD_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "road",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}

_PAVED_SURFACES = {"asphalt", "paved", "concrete", "paving_stones", "concrete:plates", "concrete:lanes"}


def _parse_tags(way_tags: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in way_tags.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def classify(way_tags: str) -> Klass:
    """Map a BRouter WayTags string to a surface class."""
    if not way_tags or way_tags == "-":
        return "unknown"
    t = _parse_tags(way_tags)
    highway = t.get("highway", "")
    bicycle = t.get("bicycle", "")
    surface = t.get("surface", "")

    if highway == "cycleway":
        return "cycleway"
    if bicycle == "designated" and highway in {"path", "footway"}:
        return "cycleway"
    if highway in {"path", "footway", "pedestrian", "steps"}:
        return "path"
    if highway == "track":
        return "road" if surface in _PAVED_SURFACES else "track"
    if highway in _ROAD_HIGHWAYS:
        return "road"
    return "unknown"


async def fetch_segments(
    waypoints: list[tuple[float, float]],
    profile: str,
    alternative_idx: int,
) -> list[dict]:
    """Fetch BRouter CSV and return [{lat, lon, dist_to_prev_m, klass}] per point.

    First row's klass labels the segment from the snap-start to row 1's coord
    (small approximation; impact ~10m of a route).
    """
    lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    params = {
        "lonlats": lonlats,
        "profile": profile,
        "alternativeidx": alternative_idx,
        "format": "csv",
    }
    resp = await client().get(BROUTER_URL, params=params)
    resp.raise_for_status()
    lines = resp.text.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    col = {name: i for i, name in enumerate(header)}
    out: list[dict] = []
    for raw in lines[1:]:
        parts = raw.split("\t")
        if len(parts) < len(header):
            continue
        lon = int(parts[col["Longitude"]]) / 1e6
        lat = int(parts[col["Latitude"]]) / 1e6
        dist = int(parts[col["Distance"]]) if parts[col["Distance"]] else 0
        out.append(
            {
                "lat": lat,
                "lon": lon,
                "dist_to_prev_m": dist,
                "klass": classify(parts[col["WayTags"]]),
            }
        )
    return out


def project_to_gpx(
    csv_segments: list[dict], gpx_points: list[tuple[float, float]]
) -> list[dict]:
    """Map each GPX trackpoint to a class via cumulative-distance alignment with CSV.

    Reason: BRouter's GPX has ~4x more points than its CSV (full OSM way shape vs
    routing decision points). Rendering with CSV coords cuts curves; we want the
    GPX shape with CSV-derived classes.

    Returns: [{lat, lon, klass, dist_to_prev_m}] aligned to gpx_points length.
    """
    if not csv_segments or not gpx_points:
        return []

    csv_cum: list[float] = []
    t = 0.0
    for s in csv_segments:
        t += float(s.get("dist_to_prev_m", 0))
        csv_cum.append(t)
    csv_total = csv_cum[-1]

    gpx_cum: list[float] = [0.0]
    for i in range(1, len(gpx_points)):
        gpx_cum.append(
            gpx_cum[-1]
            + haversine_m(
                gpx_points[i - 1][0], gpx_points[i - 1][1],
                gpx_points[i][0], gpx_points[i][1],
            )
        )
    gpx_total = gpx_cum[-1]

    if csv_total <= 0 or gpx_total <= 0:
        return []

    scale = csv_total / gpx_total
    out: list[dict] = []
    j = 0
    last_j = len(csv_segments) - 1
    for i, (lat, lon) in enumerate(gpx_points):
        target = gpx_cum[i] * scale
        while j < last_j and csv_cum[j] < target:
            j += 1
        seg_dist = gpx_cum[i] - gpx_cum[i - 1] if i > 0 else 0.0
        out.append(
            {
                "lat": lat,
                "lon": lon,
                "klass": csv_segments[j]["klass"],
                "dist_to_prev_m": seg_dist,
            }
        )
    return out


def group_lines(points: list[dict]) -> list[dict]:
    """Group consecutive same-class points into LineString-style coord lists.

    Adjacent groups share their boundary point so the rendered line stays continuous.
    """
    if not points:
        return []
    groups: list[dict] = []
    cur_klass = points[0]["klass"]
    coords: list[list[float]] = [[points[0]["lon"], points[0]["lat"]]]
    for p in points[1:]:
        if p["klass"] != cur_klass:
            coords.append([p["lon"], p["lat"]])
            groups.append({"klass": cur_klass, "coords": coords})
            cur_klass = p["klass"]
            coords = [[p["lon"], p["lat"]]]
        else:
            coords.append([p["lon"], p["lat"]])
    if coords:
        groups.append({"klass": cur_klass, "coords": coords})
    return groups


def breakdown(points: list[dict]) -> dict[str, float]:
    """Distance share per class in percent (sums to ~100)."""
    totals: dict[str, float] = {k: 0.0 for k in ALL_KLASSES}
    total = 0.0
    for p in points:
        d = float(p.get("dist_to_prev_m", 0))
        totals[p["klass"]] += d
        total += d
    if total <= 0:
        return {f"{k}_pct": 0.0 for k in ALL_KLASSES}
    return {f"{k}_pct": round(totals[k] / total * 100, 1) for k in ALL_KLASSES}
