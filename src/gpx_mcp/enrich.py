"""Surface reclassification for existing GPX files via Overpass.

For routes built before surface classification existed (or for externally
imported GPX), this re-derives per-trackpoint way tags from OSM and writes a
sidecar matching the GPX's full geometry.
"""
from __future__ import annotations

import json
from math import cos, radians
from pathlib import Path

from .http import client
from .metrics import extract_latlon, haversine_m
from .surface import breakdown, classify, group_lines

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
SNAP_RADIUS_M = 20.0
GRID_CELL_DEG = 0.002  # ~200 m at central Europe latitudes
BBOX_BUFFER_DEG = 0.003


async def _fetch_ways(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> list[dict]:
    query = (
        f"[out:json][timeout:90];\n"
        f'way["highway"]({min_lat:.6f},{min_lon:.6f},{max_lat:.6f},{max_lon:.6f});\n'
        f"out geom tags;"
    )
    resp = await client().post(OVERPASS_URL, data={"data": query}, timeout=180.0)
    resp.raise_for_status()
    return resp.json().get("elements", [])


def _tags_str(tags: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in tags.items())


def _build_segments(ways: list[dict]) -> list[tuple[float, float, float, float, str]]:
    segs: list[tuple[float, float, float, float, str]] = []
    for w in ways:
        klass = classify(_tags_str(w.get("tags", {})))
        geom = w.get("geometry") or []
        for i in range(len(geom) - 1):
            a, b = geom[i], geom[i + 1]
            segs.append((a["lat"], a["lon"], b["lat"], b["lon"], klass))
    return segs


def _segment_cells(seg: tuple[float, float, float, float, str]) -> set[tuple[int, int]]:
    """Cells touched by the segment (sample at GRID_CELL_DEG / 2 resolution)."""
    lat1, lon1, lat2, lon2, _ = seg
    steps = max(2, int(max(abs(lat1 - lat2), abs(lon1 - lon2)) / (GRID_CELL_DEG / 2)) + 1)
    cells: set[tuple[int, int]] = set()
    for i in range(steps):
        t = i / (steps - 1)
        lat = lat1 + t * (lat2 - lat1)
        lon = lon1 + t * (lon2 - lon1)
        cells.add((int(lat / GRID_CELL_DEG), int(lon / GRID_CELL_DEG)))
    return cells


def _build_grid(segs: list[tuple]) -> dict[tuple[int, int], list[tuple]]:
    grid: dict[tuple[int, int], list[tuple]] = {}
    for s in segs:
        for cell in _segment_cells(s):
            grid.setdefault(cell, []).append(s)
    return grid


def _point_segment_dist_m(
    plat: float, plon: float,
    alat: float, alon: float,
    blat: float, blon: float,
    ref_lat_cos: float,
) -> float:
    py = plat * 111000.0
    px = plon * 111000.0 * ref_lat_cos
    ay = alat * 111000.0
    ax = alon * 111000.0 * ref_lat_cos
    by = blat * 111000.0
    bx = blon * 111000.0 * ref_lat_cos
    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    nx = ax + t * dx
    ny = ay + t * dy
    return ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5


def _classify_points(
    points: list[tuple[float, float]],
    grid: dict[tuple[int, int], list[tuple]],
    ref_lat_cos: float,
) -> list[dict]:
    out: list[dict] = []
    for i, (lat, lon) in enumerate(points):
        cx, cy = int(lat / GRID_CELL_DEG), int(lon / GRID_CELL_DEG)
        best_d = float("inf")
        best_k = "unknown"
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for seg in grid.get((cx + dx, cy + dy), ()):
                    d = _point_segment_dist_m(lat, lon, seg[0], seg[1], seg[2], seg[3], ref_lat_cos)
                    if d < best_d:
                        best_d = d
                        best_k = seg[4]
        if best_d > SNAP_RADIUS_M:
            best_k = "unknown"
        seg_dist = (
            haversine_m(points[i - 1][0], points[i - 1][1], lat, lon) if i > 0 else 0.0
        )
        out.append({"lat": lat, "lon": lon, "klass": best_k, "dist_to_prev_m": seg_dist})
    return out


def _sidecar_path(gpx_path: Path) -> Path:
    if gpx_path.suffix.lower() == ".gpx":
        return gpx_path.with_suffix(".surface.json")
    return gpx_path.parent / f"{gpx_path.name}.surface.json"


async def reclassify_gpx(gpx_path: str) -> dict:
    """Rebuild surface sidecar for an existing GPX via Overpass lookup."""
    p = Path(gpx_path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(p)

    gpx_text = p.read_text(encoding="utf-8")
    points = extract_latlon(gpx_text)
    if not points:
        raise ValueError("GPX has no trackpoints")

    lats = [pt[0] for pt in points]
    lons = [pt[1] for pt in points]
    bbox = (
        min(lats) - BBOX_BUFFER_DEG,
        min(lons) - BBOX_BUFFER_DEG,
        max(lats) + BBOX_BUFFER_DEG,
        max(lons) + BBOX_BUFFER_DEG,
    )
    ref_lat_cos = cos(radians((bbox[0] + bbox[2]) / 2))

    ways = await _fetch_ways(*bbox)
    segs = _build_segments(ways)
    grid = _build_grid(segs)

    classified = _classify_points(points, grid, ref_lat_cos)
    groups = group_lines(classified)
    breakdown_data = breakdown(classified)

    sidecar = _sidecar_path(p)
    sidecar.write_text(
        json.dumps(
            {"version": 1, "surface_breakdown": breakdown_data, "groups": groups},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    return {
        "path": str(p),
        "sidecar": str(sidecar),
        "n_trackpoints": len(points),
        "n_ways_fetched": len(ways),
        "n_segments": len(segs),
        "surface_breakdown": breakdown_data,
        "n_groups": len(groups),
        "snap_radius_m": SNAP_RADIUS_M,
    }
