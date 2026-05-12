"""Mini route viewer — Starlette + MapLibre.

Run: `uv run gpx-mcp-web`, then open http://127.0.0.1:7654
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import gpxpy
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .metrics import compute_metrics

STATIC_DIR = Path(__file__).parent / "static"
ROUTES_DIR = Path(os.environ.get("GPX_MCP_ROUTES_DIR", Path.cwd() / "data" / "routes"))


def _gpx_to_geojson(gpx_text: str) -> dict:
    parsed = gpxpy.parse(gpx_text)
    coords: list[list[float]] = []
    for trk in parsed.tracks:
        for seg in trk.segments:
            for p in seg.points:
                coords.append([p.longitude, p.latitude])
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {},
    }


def _surface_feature_collection(groups: list[dict]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": g["coords"]},
                "properties": {"klass": g["klass"]},
            }
            for g in groups
        ],
    }


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def list_routes(_: Request) -> JSONResponse:
    if not ROUTES_DIR.is_dir():
        return JSONResponse([])
    files = sorted(ROUTES_DIR.glob("*.gpx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse(
        [
            {
                "name": p.name,
                "modified": p.stat().st_mtime,
                "size_kb": round(p.stat().st_size / 1024, 1),
                "has_surface": p.with_suffix(".surface.json").is_file(),
            }
            for p in files
        ]
    )


async def get_route(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    target = (ROUTES_DIR / name).resolve()
    if not target.is_file() or ROUTES_DIR.resolve() not in target.parents:
        return JSONResponse({"error": "not found"}, status_code=404)
    gpx_text = target.read_text(encoding="utf-8")
    metadata = compute_metrics(gpx_text)

    surface_payload: dict | None = None
    sidecar = target.with_suffix(".surface.json")
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            metadata["surface_breakdown"] = data.get("surface_breakdown", {})
            surface_payload = _surface_feature_collection(data.get("groups", []))
        except json.JSONDecodeError:
            surface_payload = None

    return JSONResponse(
        {
            "name": name,
            "metadata": metadata,
            "geojson": _gpx_to_geojson(gpx_text),
            "surface": surface_payload,
        }
    )


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/routes", list_routes),
        Route("/api/routes/{name}", get_route),
    ]
)


def main() -> None:
    host = os.environ.get("GPX_MCP_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("GPX_MCP_WEB_PORT", "7654"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
