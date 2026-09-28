"""FastMCP server: GPX handling + map information for bike route planning.

The server is a toolbox, not a planner: it routes between the waypoints it is
given, measures and describes tracks, edits them, and answers map questions
(POIs, stations, cycle routes, weather). Deciding *where* to go is the agent's job.

Run: `uv run gpx-mcp` (stdio transport).

Handle pattern: `route`, `load_gpx` and the edit tools return a short
`route_id` plus compact metadata; the full GPX stays server-side.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import duration as duration_mod
from . import edit as edit_mod
from . import enrich as enrich_mod
from . import geocode as geocode_mod
from . import gpx_utils
from . import pois as pois_mod
from . import routing as routing_mod
from . import sections as sections_mod
from . import state
from . import stations as stations_mod
from . import weather as weather_mod
from .metrics import interpolate_at_distance, nearest_index

mcp = FastMCP("gpx-mcp")

ROUTES_DIR = Path(os.environ.get("GPX_MCP_ROUTES_DIR", Path.cwd() / "data" / "routes"))
VIEWER_URL = os.environ.get("GPX_MCP_VIEWER_URL", "http://127.0.0.1:7654")

Category = pois_mod.Category


# --------------------------------------------------------------------------- places

@mcp.tool()
async def geocode(query: str, limit: int = 3, country_codes: str | None = "de") -> list[dict]:
    """Place name → coordinates (Nominatim). Plain names work best ("Feucht", "Hersbruck Bahnhof")."""
    return await geocode_mod.geocode(query=query, limit=limit, country_codes=country_codes)


@mcp.tool()
async def reverse_geocode(lat: float, lon: float, zoom: int = 16) -> dict:
    """Coordinates → road / place name. zoom 16 = street level, 14 = village, 10 = city."""
    return await geocode_mod.reverse(lat, lon, zoom=zoom)


# --------------------------------------------------------------------------- routing

@mcp.tool()
async def route(
    waypoints: list[tuple[float, float]],
    profile: routing_mod.Profile = "trekking",
    alternative_idx: int = 0,
    nogos: list[tuple[float, float, float]] | None = None,
    nogo_polygons: list[list[tuple[float, float]]] | None = None,
) -> dict:
    """Bike route through waypoints [(lat, lon), ...] via BRouter. Returns route_id + metadata.

    Profiles: trekking (default, prefers cycleways) · trekking-noferries · trekking-steep
    (accepts steeper climbs for shorter routes) · trekking-ignore-cr (ignores signed cycle
    routes) · safety (max cycleway preference) · fastbike / fastbike-lowtraffic /
    fastbike-verylowtraffic (road bike, paved) · shortest · gravel.
    alternative_idx 0..3 asks BRouter for alternatives to the same waypoints.

    Shaping tools:
      nogos: [(lat, lon, radius_m), ...] circular areas the route must avoid
             (a busy road segment, a closed bridge, a town centre).
      nogo_polygons: [[(lat, lon), ...], ...] polygonal avoid areas.

    Metadata: distance_km, ascent_m, descent_m, max_grade_percent, steep_uphill_m,
    climbs (biggest, with km/gain/grade), elevation_profile [[km, ele], ...],
    sampled_geometry [[lat, lon], ...], bbox, surface_breakdown (cycleway/road/track/path
    shares, paved vs unpaved, busy vs quiet roads, steps, ferry, signed cycle-route share),
    hazards (steps, barriers, level crossings with km), brouter_time_min.
    """
    return await routing_mod.route(
        waypoints=waypoints, profile=profile, alternative_idx=alternative_idx,
        nogos=nogos, nogo_polygons=nogo_polygons,
    )


@mcp.tool()
def analyze_gpx(route_id: str) -> dict:
    """Metadata for a cached route_id (same shape as `route` returns)."""
    return state.get(route_id).metadata


@mcp.tool()
async def describe_section(
    route_id: str,
    from_km: float | None = None,
    to_km: float | None = None,
    min_run_m: float = 150.0,
    with_places: bool = True,
) -> dict:
    """What is the route like between from_km and to_km (default: whole route)?

    Returns section stats, climbs inside the range, hazards (steps, barriers, crossings),
    `places` (reverse-geocoded road/place names at a few km marks; costs ~1 s each) and
    `runs`: consecutive stretches with the same character (highway type, klass, paved,
    surface, traffic exposure, cycle-route membership) with km bounds and avg grade.
    Use it to answer "where exactly is the busy road / the gravel / the climb?".
    """
    return await sections_mod.describe(route_id, from_km, to_km, min_run_m, with_places)


@mcp.tool()
def compare_routes(route_ids: list[str]) -> list[dict]:
    """Side-by-side key figures for several cached routes (one compact row each)."""
    rows = []
    for rid in route_ids:
        m = state.get(rid).metadata
        sb = m.get("surface_breakdown") or {}
        rows.append(
            {
                "route_id": rid,
                "profile": m.get("profile"),
                "alt": m.get("alternative_idx"),
                "edit": m.get("edit"),
                "distance_km": m.get("distance_km"),
                "ascent_m": m.get("ascent_m"),
                "max_grade_pct": m.get("max_grade_percent"),
                "steep_uphill_m": m.get("steep_uphill_m"),
                "n_climbs": m.get("n_climbs"),
                "cycleway_pct": sb.get("cycleway_pct"),
                "unpaved_pct": sb.get("unpaved_pct"),
                "busy_road_pct": sb.get("busy_road_pct"),
                "signed_cycle_route_pct": sb.get("signed_cycle_route_pct"),
                "steps_count": sb.get("steps_count"),
                "brouter_time_min": m.get("brouter_time_min"),
            }
        )
    return rows


@mcp.tool()
def point_at_km(route_id: str, km: float) -> dict:
    """Coordinates + elevation at a km mark of a cached route (e.g. to place a via point or nogo)."""
    c = state.get(route_id)
    return interpolate_at_distance(c.pts, c.cum, c.elev, km * 1000)


@mcp.tool()
def locate_on_route(route_id: str, lat: float, lon: float) -> dict:
    """Where along a cached route is (lat, lon)? Returns km_position and distance to the track."""
    c = state.get(route_id)
    idx, d = nearest_index(c.pts, lat, lon)
    return {"km_position": round(c.cum[idx] / 1000, 2), "distance_to_track_m": round(d), "ele_m": round(c.elev[idx])}


# --------------------------------------------------------------------------- editing

@mcp.tool()
def reverse_route(route_id: str) -> dict:
    """Reverse a cached route (new route_id). Note: BRouter routes are not symmetric — one-way
    streets and side-of-road cycleways differ; re-route the reversed waypoints if that matters."""
    return edit_mod.reverse(route_id)


@mcp.tool()
def trim_route(route_id: str, from_km: float, to_km: float) -> dict:
    """Cut a cached route to the km range [from_km, to_km] (new route_id). For stages / abort points."""
    return edit_mod.trim(route_id, from_km, to_km)


@mcp.tool()
def concat_routes(route_ids: list[str]) -> dict:
    """Join cached routes end-to-start into one (new route_id). Reports gaps between parts."""
    return edit_mod.concat(route_ids)


# --------------------------------------------------------------------------- files

@mcp.tool()
def save_gpx(
    route_id: str,
    name: str,
    description: str | None = None,
    waypoints: list[dict] | None = None,
) -> dict:
    """Write a cached route to data/routes/<name>.gpx (+ surface sidecar).

    `description` goes into the GPX metadata. `waypoints` become <wpt> markers that
    Komoot / Garmin / OsmAnd show on the map: [{lat, lon, name, description?, type?}, ...]
    (e.g. cafés and stations you picked from find_pois_along_route).
    """
    path = gpx_utils.save_gpx(route_id, name, ROUTES_DIR, description=description, waypoints=waypoints)
    return {
        "path": path,
        "name": Path(path).name,
        "route_id": route_id,
        "viewer_url": f"{VIEWER_URL}/?route={Path(path).name}",
    }


@mcp.tool()
def load_gpx(path: str) -> dict:
    """Import a GPX file into the cache. Returns metadata + route_id.
    Without a sidecar the surface info is missing — run reclassify_surface afterwards."""
    return gpx_utils.load_gpx(path)


@mcp.tool()
def list_routes() -> dict:
    """Cached route_ids (this session) and GPX files saved in the routes directory."""
    return {"cached": state.summaries(), "saved": gpx_utils.list_saved(ROUTES_DIR), "routes_dir": str(ROUTES_DIR)}


@mcp.tool()
def delete_saved_route(name: str) -> dict:
    """Delete a saved GPX (and its sidecar) from the routes directory."""
    return gpx_utils.delete_saved(name, ROUTES_DIR)


@mcp.tool()
async def reclassify_surface(route_id: str | None = None, path: str | None = None) -> dict:
    """Derive surface / traffic / cycle-route info for an imported GPX from OSM (Overpass).

    Pass a cached `route_id` (or a `path`, which is loaded first). Updates the cache in
    place and writes the sidecar next to the source file. Takes ~10-60 s depending on length.
    """
    return await enrich_mod.reclassify(route_id=route_id, path=path)


# --------------------------------------------------------------------------- map information

@mcp.tool()
async def find_pois(
    lat: float,
    lon: float,
    category: Category | None = None,
    custom_filter: str | None = None,
    radius_m: int = 5000,
    limit: int = 30,
) -> list[dict]:
    """POIs around a point (Overpass), sorted by distance.

    Either a `category` (forest, viewpoint, cafe, bakery, restaurant, biergarten, supermarket,
    lake, swimming, peak, castle, rest_area, shelter, picnic, drinking_water, toilets, bike_shop,
    bike_repair, charging_station, campsite, accommodation, fuel, ferry, attraction) or a raw
    `custom_filter` of Overpass tag filters, e.g. ["amenity"="cafe"]["outdoor_seating"="yes"].
    """
    return await pois_mod.find_pois(lat, lon, category, custom_filter, radius_m, limit)


@mcp.tool()
async def find_pois_along_route(
    route_id: str,
    category: Category | None = None,
    custom_filter: str | None = None,
    max_detour_m: int = 1000,
    limit: int = 50,
    from_km: float | None = None,
    to_km: float | None = None,
) -> list[dict]:
    """POIs within max_detour_m of a cached route, sorted by km_position.

    Same categories / custom_filter as find_pois. Each hit carries `km_position` and
    `detour_m`. Optional from_km/to_km restrict the search to part of the route.
    """
    return await pois_mod.find_pois_along_route(
        route_id, category, custom_filter, max_detour_m, limit, from_km, to_km
    )


@mcp.tool()
async def find_cycle_routes(
    lat: float | None = None,
    lon: float | None = None,
    route_id: str | None = None,
    radius_m: int = 2000,
    limit: int = 30,
) -> list[dict]:
    """Signed cycle routes (OSM route=bicycle relations, e.g. "Fünf-Flüsse-Radweg") near a point
    or along a cached route, ordered international → national → regional → local."""
    return await pois_mod.find_cycle_routes(lat, lon, route_id, radius_m, limit)


@mcp.tool()
async def find_train_stations(
    lat: float | None = None,
    lon: float | None = None,
    route_id: str | None = None,
    radius_m: int = 10000,
    max_detour_m: int = 3000,
    limit: int = 30,
) -> list[dict]:
    """Train stations + halts around (lat, lon) or along a cached route (route_id).

    `likely_bike_friendly` is a soft OSM-tag heuristic (regional / S-Bahn = usually yes).
    Verify bike carriage rules in DB Navigator before relying on it.
    """
    if route_id is not None:
        return await stations_mod.find_stations_along_route(route_id, max_detour_m, limit)
    if lat is None or lon is None:
        raise ValueError("Provide either route_id or (lat, lon).")
    return await stations_mod.find_stations_around(lat, lon, radius_m, limit)


@mcp.tool()
async def weather_forecast(lat: float, lon: float, date: str, from_hour: int = 6, to_hour: int = 20) -> dict:
    """Hourly forecast (Open-Meteo) for a day (YYYY-MM-DD, ≤16 days ahead): temperature, rain
    probability, wind speed/gusts and wind direction (where it blows FROM, e.g. "W" = tailwind
    when riding east). Query start and end of a long route separately."""
    return await weather_mod.forecast(lat, lon, date, from_hour, to_hour)


@mcp.tool()
async def elevation_at(points: list[tuple[float, float]]) -> list[dict]:
    """Terrain elevation for up to 100 (lat, lon) points (Open-Meteo DEM). For candidate via points."""
    return await weather_mod.elevation(points)


@mcp.tool()
def estimate_duration(route_id: str, avg_speed_kmh: float = 18.0, break_minutes_per_hour: float = 5.0) -> dict:
    """Ride-time estimate from the elevation profile. `avg_speed_kmh` is the flat cruising speed;
    grades adjust it piecewise (descents capped). Compare with metadata.brouter_time_min."""
    return duration_mod.estimate(route_id, avg_speed_kmh, break_minutes_per_hour)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
