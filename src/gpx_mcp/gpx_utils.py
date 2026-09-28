"""GPX build / save / load and the surface sidecar (`<name>.surface.json`).

Sidecar v2 stores per-segment OSM tags plus a trackpoint→segment map, so a
re-imported GPX keeps its full classification (and can be edited). v1
sidecars (groups only) still load for the viewer.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import gpxpy
import gpxpy.gpx

from . import state
from .metrics import compute_metrics
from .surface import breakdown, classify_tags, group_lines

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
SIDECAR_VERSION = 2


def sidecar_path(gpx_path: Path) -> Path:
    if gpx_path.suffix.lower() == ".gpx":
        return gpx_path.with_suffix(".surface.json")
    return gpx_path.parent / f"{gpx_path.name}.surface.json"


def safe_name(name: str) -> str:
    safe = _SAFE_NAME.sub("_", name).strip("_") or "route"
    if not safe.lower().endswith(".gpx"):
        safe += ".gpx"
    return safe


def build_gpx(
    points: list[tuple[float, float, float | None]],
    name: str | None = None,
    description: str | None = None,
    waypoints: list[dict] | None = None,
) -> str:
    """Serialize (lat, lon, ele) points as a single-track GPX, optionally with <wpt>."""
    g = gpxpy.gpx.GPX()
    g.creator = "gpx-mcp"
    g.name = name
    g.description = description
    g.time = datetime.now(timezone.utc)
    trk = gpxpy.gpx.GPXTrack(name=name, description=description)
    seg = gpxpy.gpx.GPXTrackSegment()
    for lat, lon, ele in points:
        seg.points.append(gpxpy.gpx.GPXTrackPoint(lat, lon, elevation=ele))
    trk.segments.append(seg)
    g.tracks.append(trk)
    for w in waypoints or []:
        g.waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=w["lat"],
                longitude=w["lon"],
                elevation=w.get("ele_m"),
                name=w.get("name"),
                description=w.get("description"),
                symbol=w.get("symbol"),
                type=w.get("type"),
            )
        )
    return g.to_xml()


def with_metadata(gpx_text: str, name: str | None, description: str | None, waypoints: list[dict] | None) -> str:
    """Return gpx_text with name/description/waypoints set (track points untouched)."""
    if name is None and description is None and not waypoints:
        return gpx_text
    g = gpxpy.parse(gpx_text)
    if name is not None:
        g.name = name
        for trk in g.tracks:
            trk.name = name
    if description is not None:
        g.description = description
        for trk in g.tracks:
            trk.description = description
    for w in waypoints or []:
        g.waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=w["lat"], longitude=w["lon"], elevation=w.get("ele_m"),
                name=w.get("name"), description=w.get("description"),
                symbol=w.get("symbol"), type=w.get("type"),
            )
        )
    return g.to_xml()


def _write_sidecar(out: Path, cached: state.CachedRoute) -> None:
    pts = [(p.latitude, p.longitude) for p in cached.pts]
    payload = {
        "version": SIDECAR_VERSION,
        "surface_breakdown": cached.metadata.get("surface_breakdown", {}),
        "groups": group_lines(pts, cached.klasses()),
        "segments": [
            {"d": s["dist_m"], "t": s.get("tags", {}), "n": s.get("node_tags", {})}
            for s in cached.segments
        ],
        "point_seg": cached.point_seg,
    }
    sidecar_path(out).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _read_sidecar(p: Path) -> tuple[list[dict], list[int], dict | None]:
    sc = sidecar_path(p)
    if not sc.is_file():
        return [], [], None
    try:
        payload = json.loads(sc.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], [], None
    bd = payload.get("surface_breakdown")
    if payload.get("version", 1) < 2:
        return [], [], bd
    segments = []
    for s in payload.get("segments", []):
        seg = {"dist_m": s.get("d", 0), "tags": s.get("t", {}), "node_tags": s.get("n", {})}
        seg.update(classify_tags(seg["tags"]))
        segments.append(seg)
    return segments, payload.get("point_seg", []), bd


def save_gpx(
    route_id: str,
    name: str,
    routes_dir: Path,
    description: str | None = None,
    waypoints: list[dict] | None = None,
) -> str:
    """Persist a cached route's GPX (and sidecar) to disk. Returns the absolute path."""
    cached = state.get(route_id)
    routes_dir.mkdir(parents=True, exist_ok=True)
    out = routes_dir / safe_name(name)
    title = name[:-4] if name.lower().endswith(".gpx") else name
    out.write_text(with_metadata(cached.gpx, title, description, waypoints), encoding="utf-8")
    cached.metadata["name"] = title
    if cached.has_surface:
        _write_sidecar(out, cached)
    return str(out.resolve())


def load_gpx(path: str) -> dict:
    """Import a GPX file (and sidecar if present). Returns metadata + route_id."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"GPX file not found: {p}")
    gpx_text = p.read_text(encoding="utf-8")
    metadata = compute_metrics(gpx_text)
    metadata["source_path"] = str(p)
    parsed = gpxpy.parse(gpx_text)
    metadata["name"] = parsed.name or (parsed.tracks[0].name if parsed.tracks else None) or p.stem
    if parsed.waypoints:
        metadata["waypoints_in_file"] = [
            {"lat": w.latitude, "lon": w.longitude, "name": w.name} for w in parsed.waypoints[:50]
        ]

    segments, point_seg, bd = _read_sidecar(p)
    if segments and len(point_seg) == metadata["n_points"]:
        metadata["surface_breakdown"] = breakdown(segments)
    else:
        segments, point_seg = [], []
        if bd:
            metadata["surface_breakdown"] = bd
        else:
            metadata["surface_breakdown"] = None
            metadata["hint"] = "No surface info. Run reclassify_surface(path) to derive it from OSM."

    route_id = state.store(gpx_text, metadata, segments=segments, point_seg=point_seg)
    return state.get(route_id).metadata


def list_saved(routes_dir: Path) -> list[dict]:
    if not routes_dir.is_dir():
        return []
    out = []
    for p in sorted(routes_dir.glob("*.gpx"), key=lambda q: q.stat().st_mtime, reverse=True):
        out.append(
            {
                "name": p.name,
                "path": str(p.resolve()),
                "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes"),
                "size_kb": round(p.stat().st_size / 1024, 1),
                "has_surface": sidecar_path(p).is_file(),
            }
        )
    return out


def delete_saved(name: str, routes_dir: Path) -> dict:
    target = (routes_dir / safe_name(name)).resolve()
    if routes_dir.resolve() not in target.parents:
        raise ValueError("Refusing to delete outside the routes directory.")
    if not target.is_file():
        raise FileNotFoundError(f"No saved route named {target.name}")
    target.unlink()
    sc = sidecar_path(target)
    removed_sidecar = False
    if sc.is_file():
        sc.unlink()
        removed_sidecar = True
    return {"deleted": target.name, "sidecar_removed": removed_sidecar}
