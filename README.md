# gpx-mcp

[![Built with Claude Code](https://img.shields.io/badge/built%20with-Claude%20Code-d97757)](https://claude.com/claude-code)

MCP server for planning bike routes. Wraps **BRouter**, **Nominatim**, and the
**Overpass API** so a Claude (or other MCP client) can geocode places, compute
GPX tracks, find POIs and train stations, classify surface coverage, and
estimate ride time — all from a chat.

A small HTTP route viewer (Starlette + MapLibre) is included for visual
inspection of saved tracks.

## Tools

The server exposes 11 MCP tools:

| Tool | What it does |
| --- | --- |
| `geocode` | Resolve a place name to coordinates via Nominatim. |
| `route` | Compute a bike route through waypoints (BRouter). Returns a `route_id` + metadata; full GPX stays server-side. |
| `analyze_gpx` | Re-read metadata for a cached `route_id`. |
| `save_gpx` | Persist a cached route to `data/routes/<name>.gpx` (+ surface sidecar). |
| `load_gpx` | Import an external GPX into the cache. |
| `reclassify_surface` | Rebuild the surface sidecar for a saved GPX via Overpass. |
| `find_pois` | POIs (forest, viewpoint, cafe, lake, peak, castle, rest_area, drinking_water) around a point. |
| `find_pois_along_route` | POIs within `max_detour_m` of a cached route, sorted by km-position. |
| `find_train_stations` | Stations + halts around a point or along a route, with a `likely_bike_friendly` heuristic. |
| `estimate_duration` | Ride-time estimate from the elevation profile (piecewise grade-adjusted speed). |
| `suggest_detour` | Suggest one extra waypoint to add ~N km via a POI category. |

### Handle pattern

`route` and `load_gpx` return a short `route_id` plus a compact metadata dict
(distance, ascent, max grade, surface breakdown, sampled geometry, bbox).
The full GPX stays in an in-memory LRU cache; downstream tools work on the
handle. This keeps agent context lean even for 200 km tours.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                  # MCP server only
uv sync --extra viewer   # + the web route viewer
```

The MCP server is the core of this repo; the web viewer is a bonus and its
dependencies (`starlette`, `uvicorn`) are gated behind the `viewer` extra.

## Run as MCP server (stdio)

```sh
uv run gpx-mcp
```

Wire it into Claude Code / Claude Desktop:

```json
{
  "mcpServers": {
    "gpx-mcp": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/gpx-mcp", "run", "gpx-mcp"]
    }
  }
}
```

Saved routes land in `./data/routes/` by default. Override with
`GPX_MCP_ROUTES_DIR=/your/path`.

## Run the web viewer

Requires the `viewer` extra (see Setup).

```sh
uv run gpx-mcp-web
```

Then open <http://127.0.0.1:7654>. Lists saved tracks from
`data/routes/`, renders the GPX, and color-codes segments by surface class
(cycleway / road / track / path / unknown) when a `.surface.json` sidecar exists.

## External services

This server talks to three free OSM-based services. Be a good citizen:

- **BRouter** (`brouter.de`) — bike routing. Profiles used: `trekking`,
  `safety`, `fastbike`, `shortest`, `trekking-ignore-cr`. Public instance has
  no documented hard rate limit; keep it reasonable.
- **Nominatim** (`nominatim.openstreetmap.org`) — geocoding. Hard 1 req/s
  fair-use policy; the server throttles internally.
- **Overpass** (`overpass-api.de`) — POI / surface / station queries. Avoid
  pathological queries; the server scopes everything to small bboxes or
  buffered route geometries.

All requests carry an identifying `User-Agent` (see `src/gpx_mcp/http.py`).

## Example prompts

> "Plane mir eine Tour von Nürnberg nach Feucht über Wald, ohne Steigungen
> über 6 %. Speichere sie als `nbg-feucht-flach`."

> "Gibt es entlang der Tour Cafés alle ~20 km und Bahnhöfe für eine
> mögliche Abkürzung zurück?"

> "Wie lange brauche ich für die Strecke bei 22 km/h auf der Ebene?"

## Project layout

```
src/gpx_mcp/
  server.py        FastMCP entry point + tool registration
  routing.py       BRouter integration
  geocode.py       Nominatim geocoding with throttle
  pois.py          POI lookup (point + along-route)
  stations.py      Train station lookup with bike-friendly heuristic
  duration.py      Ride-time estimation
  detour.py        Detour suggestion heuristic
  surface.py       BRouter CSV → surface class breakdown
  enrich.py        Surface sidecar rebuild for existing GPX
  metrics.py       GPX geometry / elevation / grade primitives
  state.py         In-memory route cache (handle pattern)
  http.py          Shared async HTTP client
  gpx_utils.py     Disk I/O for GPX + sidecar
  viewer/          Bonus: Starlette route viewer (optional dep)
    web.py
    static/
```

## How this was made

The code, tests and docs were written by Claude (Anthropic) with Claude
Code, directed and reviewed by me. The tools are covered by the test suite in
`tests/`.
