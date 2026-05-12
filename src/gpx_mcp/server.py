"""FastMCP server for bike route planning.

Run: `uv run gpx-mcp` (stdio transport, ready for Claude Code / Desktop).

Design: route() and load_gpx() return small metadata dicts plus a route_id.
The full GPX is kept server-side; downstream tools (save_gpx, analyze_gpx)
operate on the route_id. This keeps agent context lean.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import detour as detour_mod
from . import enrich as enrich_mod
from . import geocode as geocode_mod
from . import pois as pois_mod
from . import routing as routing_mod
from . import state
from .gpx_utils import load_gpx as load_gpx_impl
from .gpx_utils import save_gpx as save_gpx_impl

mcp = FastMCP("gpx-mcp")

ROUTES_DIR = Path(os.environ.get("GPX_MCP_ROUTES_DIR", Path.cwd() / "data" / "routes"))


@mcp.tool()
async def geocode(query: str, limit: int = 3, country_codes: str | None = "de") -> list[dict]:
    """Resolve a place name to coordinates via Nominatim (OpenStreetMap).

    Use plain names like "Nürnberg" or "Feucht". Avoid Regierungsbezirk names
    like "Mittelfranken" — Nominatim won't match those reliably.
    """
    return await geocode_mod.geocode(query=query, limit=limit, country_codes=country_codes)


@mcp.tool()
async def route(
    waypoints: list[tuple[float, float]],
    profile: Literal["trekking", "safety", "fastbike", "shortest", "trekking-ignore-cr"] = "trekking",
    alternative_idx: int = 0,
) -> dict:
    """Compute a bike route through waypoints (BRouter). Returns metadata + route_id.

    Profiles:
      - `trekking` (default): strongly prefers cycleways, balanced. Best for "überwiegend Radwege".
      - `safety`: extreme cycleway preference, may detour heavily.
      - `fastbike`: road-bike oriented, paved priority.
      - `shortest`: minimal distance, ignores comfort.

    Returned fields:
      - route_id: opaque handle for save_gpx / future tools (GPX stays server-side)
      - distance_km, ascent_m, descent_m, n_points
      - max_grade_percent: steepest 50m-windowed climb in % (e.g. 8.4)
      - steep_uphill_m: cumulative meters of segments steeper than 5%
      - elevation_profile: 30 (km, elev_m) samples for shape reasoning
      - sampled_geometry: 20 (lat, lon) samples for visual sanity check
      - bbox: bounding box

    For elevation constraints ("max 800 hm", "möglichst flach", "keine Steigungen über 6%"):
    request multiple alternative_idx (0..3) and pick the one with the best
    ascent_m / max_grade_percent / steep_uphill_m. If none satisfies the
    constraint, use suggest_detour with a different poi_category to reshape
    the route (e.g. valley POIs instead of peaks).
    """
    return await routing_mod.route(
        waypoints=waypoints, profile=profile, alternative_idx=alternative_idx
    )


@mcp.tool()
async def find_pois(
    lat: float,
    lon: float,
    category: Literal[
        "forest",
        "viewpoint",
        "cafe",
        "lake",
        "peak",
        "castle",
        "rest_area",
        "drinking_water",
    ],
    radius_m: int = 5000,
    limit: int = 30,
) -> list[dict]:
    """Find points of interest of a category around (lat, lon) via Overpass."""
    return await pois_mod.find_pois(
        lat=lat, lon=lon, category=category, radius_m=radius_m, limit=limit
    )


@mcp.tool()
def analyze_gpx(route_id: str) -> dict:
    """Re-read metadata for a route_id from the server-side cache."""
    return state.get(route_id).metadata


@mcp.tool()
def save_gpx(route_id: str, name: str) -> dict:
    """Persist a cached route's GPX to disk under data/routes/{name}.gpx."""
    path = save_gpx_impl(route_id, name, ROUTES_DIR)
    return {"path": path, "name": Path(path).name, "route_id": route_id}


@mcp.tool()
def load_gpx(path: str) -> dict:
    """Import an existing GPX file from disk into the cache. Returns metadata + route_id."""
    return load_gpx_impl(path)


@mcp.tool()
async def reclassify_surface(path: str) -> dict:
    """Rebuild the .surface.json sidecar for an existing GPX file via Overpass.

    Use when:
      - GPX was created before surface classification existed (no sidecar).
      - External GPX imported via load_gpx needs farbcodierung.
      - Existing sidecar is geometrically coarse (mismatched with GPX shape).

    Reads `<path>` (a .gpx file), queries OSM for highway ways in the bbox,
    snaps each GPX trackpoint to the nearest way within 20 m, writes
    `<path>.surface.json` next to the GPX. Slow for 200 km routes (~30 s).
    """
    return await enrich_mod.reclassify_gpx(path)


@mcp.tool()
async def suggest_detour(
    current_waypoints: list[tuple[float, float]],
    target_extra_km: float,
    poi_category: Literal[
        "forest", "viewpoint", "cafe", "lake", "peak", "castle", "rest_area", "drinking_water"
    ] = "viewpoint",
) -> dict:
    """Suggest one extra waypoint to insert to add ~target_extra_km to the route.

    Heuristic: offsets perpendicular from the midpoint by ~half the extra
    distance, snaps to nearest POI. The agent should insert the returned
    candidate into the waypoint list, re-route, and verify the new length.

    For elevation-sensitive prompts: prefer `lake` or `cafe` (typically in
    valleys) to add distance without climbing; use `peak` or `castle` to add
    distance and climb deliberately.
    """
    return await detour_mod.suggest_detour(
        current_waypoints=current_waypoints,
        target_extra_km=target_extra_km,
        poi_category=poi_category,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
