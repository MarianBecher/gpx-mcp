"""save_gpx / load_gpx — file IO around the route cache."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import state
from .metrics import compute_metrics

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_SIDECAR_VERSION = 1


def _sidecar_path(gpx_path: Path) -> Path:
    if gpx_path.suffix.lower() == ".gpx":
        return gpx_path.with_suffix(".surface.json")
    return gpx_path.parent / f"{gpx_path.name}.surface.json"


def save_gpx(route_id: str, name: str, routes_dir: Path) -> str:
    """Persist a cached route's GPX (and surface sidecar if any) to disk."""
    cached = state.get(route_id)
    safe = _SAFE_NAME.sub("_", name).strip("_") or "route"
    if not safe.lower().endswith(".gpx"):
        safe += ".gpx"
    routes_dir.mkdir(parents=True, exist_ok=True)
    out = routes_dir / safe
    out.write_text(cached.gpx, encoding="utf-8")
    if cached.surface_groups:
        sidecar = _sidecar_path(out)
        sidecar.write_text(
            json.dumps(
                {
                    "version": _SIDECAR_VERSION,
                    "surface_breakdown": cached.metadata.get("surface_breakdown", {}),
                    "groups": cached.surface_groups,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    return str(out.resolve())


def load_gpx(path: str) -> dict:
    """Import an external GPX file (and sidecar if present). Returns metadata + route_id."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"GPX file not found: {p}")
    gpx_text = p.read_text(encoding="utf-8")
    metadata = compute_metrics(gpx_text)
    metadata["source_path"] = str(p)

    groups: list[dict] = []
    sidecar = _sidecar_path(p)
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            groups = payload.get("groups", [])
            if "surface_breakdown" in payload:
                metadata["surface_breakdown"] = payload["surface_breakdown"]
        except (json.JSONDecodeError, KeyError):
            pass

    route_id = state.store(gpx_text, metadata, surface_groups=groups)
    return state.get(route_id).metadata
