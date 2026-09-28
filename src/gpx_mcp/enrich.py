"""Surface (re)classification for a cached route via Overpass.

For imported GPX files (no BRouter tags) this looks up every trackpoint's
nearest OSM highway way, derives the same per-segment classification BRouter
routes get, and writes a v2 sidecar next to the source file.
"""
from __future__ import annotations

from math import cos, radians
from pathlib import Path

from . import state
from .gpx_utils import _write_sidecar, load_gpx
from .overpass import query
from .surface import breakdown, classify_tags

SNAP_RADIUS_M = 20.0
GRID_CELL_DEG = 0.002  # ~200 m
BBOX_BUFFER_DEG = 0.003
MAX_BBOX_AREA_DEG2 = 0.5  # ~ 70 km x 50 km; bigger routes are split into chunks

_NETWORK_KEY = {"icn": "route_bicycle_icn", "ncn": "route_bicycle_ncn", "rcn": "route_bicycle_rcn", "lcn": "route_bicycle_lcn"}


async def _fetch_ways(bbox: tuple[float, float, float, float]) -> tuple[list[dict], dict[int, set[str]]]:
    s, w, n, e = bbox
    ql = (
        f'way["highway"]({s:.6f},{w:.6f},{n:.6f},{e:.6f})->.w;'
        ".w out geom tags;"
        'rel(bw.w)["route"="bicycle"];out;'
    )
    data = await query(ql, timeout_s=90)
    ways: list[dict] = []
    way_networks: dict[int, set[str]] = {}
    for el in data.get("elements", []):
        if el.get("type") == "way":
            ways.append(el)
        elif el.get("type") == "relation":
            net = (el.get("tags") or {}).get("network", "")
            if net not in _NETWORK_KEY:
                continue
            for m in el.get("members", []):
                if m.get("type") == "way":
                    way_networks.setdefault(m["ref"], set()).add(net)
    return ways, way_networks


def _segments(ways: list[dict], way_networks: dict[int, set[str]]) -> list[tuple]:
    segs: list[tuple] = []
    for w in ways:
        tags = dict(w.get("tags", {}))
        for net in way_networks.get(w["id"], ()):
            tags[_NETWORK_KEY[net]] = "yes"
        geom = w.get("geometry") or []
        for i in range(len(geom) - 1):
            a, b = geom[i], geom[i + 1]
            segs.append((a["lat"], a["lon"], b["lat"], b["lon"], tags))
    return segs


def _cells(seg: tuple) -> set[tuple[int, int]]:
    lat1, lon1, lat2, lon2, _ = seg
    steps = max(2, int(max(abs(lat1 - lat2), abs(lon1 - lon2)) / (GRID_CELL_DEG / 2)) + 1)
    out: set[tuple[int, int]] = set()
    for i in range(steps):
        t = i / (steps - 1)
        out.add((int((lat1 + t * (lat2 - lat1)) / GRID_CELL_DEG), int((lon1 + t * (lon2 - lon1)) / GRID_CELL_DEG)))
    return out


def _grid(segs: list[tuple]) -> dict[tuple[int, int], list[tuple]]:
    grid: dict[tuple[int, int], list[tuple]] = {}
    for s in segs:
        for c in _cells(s):
            grid.setdefault(c, []).append(s)
    return grid


def _dist_m(plat, plon, alat, alon, blat, blon, k) -> float:
    py, px = plat * 111000.0, plon * 111000.0 * k
    ay, ax = alat * 111000.0, alon * 111000.0 * k
    by, bx = blat * 111000.0, blon * 111000.0 * k
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def _nearest_tags(lat: float, lon: float, grid, k: float) -> dict:
    cx, cy = int(lat / GRID_CELL_DEG), int(lon / GRID_CELL_DEG)
    best_d, best_t = float("inf"), {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for seg in grid.get((cx + dx, cy + dy), ()):
                d = _dist_m(lat, lon, seg[0], seg[1], seg[2], seg[3], k)
                if d < best_d:
                    best_d, best_t = d, seg[4]
    return best_t if best_d <= SNAP_RADIUS_M else {}


def _chunks(lats: list[float], lons: list[float]) -> list[tuple[float, float, float, float]]:
    """Bboxes covering the track, split along the track so each stays small."""
    n = len(lats)
    out = []
    start = 0
    while start < n:
        end = start
        s = nn = lats[start]
        w = e = lons[start]
        while end < n:
            s2, nn2 = min(s, lats[end]), max(nn, lats[end])
            w2, e2 = min(w, lons[end]), max(e, lons[end])
            if (nn2 - s2 + 2 * BBOX_BUFFER_DEG) * (e2 - w2 + 2 * BBOX_BUFFER_DEG) > MAX_BBOX_AREA_DEG2 and end > start + 10:
                break
            s, nn, w, e = s2, nn2, w2, e2
            end += 1
        out.append((s - BBOX_BUFFER_DEG, w - BBOX_BUFFER_DEG, nn + BBOX_BUFFER_DEG, e + BBOX_BUFFER_DEG))
        start = end
    return out


async def reclassify(route_id: str | None = None, path: str | None = None) -> dict:
    """Derive surface info for a cached route (or a GPX path, which is loaded first)."""
    if route_id is None:
        if path is None:
            raise ValueError("Provide route_id or path.")
        route_id = load_gpx(path)["route_id"]
    cached = state.get(route_id)
    pts, cum = cached.pts, cached.cum
    if len(pts) < 2:
        raise ValueError("Route has fewer than 2 trackpoints.")
    lats = [p.latitude for p in pts]
    lons = [p.longitude for p in pts]
    k = cos(radians(sum(lats) / len(lats)))

    ways_total = 0
    tags_per_point: list[dict] = [{}] * len(pts)
    for bbox in _chunks(lats, lons):
        ways, nets = await _fetch_ways(bbox)
        ways_total += len(ways)
        grid = _grid(_segments(ways, nets))
        for i, (lat, lon) in enumerate(zip(lats, lons)):
            if bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3] and not tags_per_point[i]:
                tags_per_point[i] = _nearest_tags(lat, lon, grid, k)

    segments: list[dict] = []
    point_seg: list[int] = []
    for i, t in enumerate(tags_per_point):
        d = cum[i] - cum[i - 1] if i > 0 else 0.0
        if segments and segments[-1]["tags"] == t:
            segments[-1]["dist_m"] += d
        else:
            seg = {"lat": lats[i], "lon": lons[i], "dist_m": d, "tags": t, "node_tags": {}}
            seg.update(classify_tags(t))
            segments.append(seg)
        point_seg.append(len(segments) - 1)
    for s in segments:
        s["dist_m"] = round(s["dist_m"])

    cached.segments = segments
    cached.point_seg = point_seg
    cached.metadata["surface_breakdown"] = breakdown(segments)
    cached.metadata.pop("hint", None)
    unmatched = sum(1 for t in tags_per_point if not t)
    cached.metadata["reclassify"] = {
        "n_ways_fetched": ways_total,
        "unmatched_points": unmatched,
        "snap_radius_m": SNAP_RADIUS_M,
    }
    src = cached.metadata.get("source_path")
    if src:
        _write_sidecar(Path(src), cached)
        cached.metadata["reclassify"]["sidecar"] = str(Path(src).with_suffix(".surface.json"))
    return cached.metadata
