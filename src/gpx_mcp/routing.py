"""Bike routing via BRouter public instance.

BRouter profiles favoring cycleways: `trekking` (default), `safety`, `fastbike`.
Docs: https://brouter.de/brouter-web/
"""
from __future__ import annotations

import asyncio
from typing import Literal

from . import state, surface
from .http import client
from .metrics import compute_metrics, extract_latlon

BROUTER_URL = "https://brouter.de/brouter"

Profile = Literal["trekking", "safety", "fastbike", "shortest", "trekking-ignore-cr"]


async def _fetch_gpx(
    waypoints: list[tuple[float, float]], profile: str, alternative_idx: int
) -> str:
    lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    params = {
        "lonlats": lonlats,
        "profile": profile,
        "alternativeidx": alternative_idx,
        "format": "gpx",
    }
    resp = await client().get(BROUTER_URL, params=params)
    if resp.status_code != 200 or not resp.text.lstrip().startswith("<?xml"):
        raise RuntimeError(
            f"BRouter request failed (status {resp.status_code}): {resp.text[:200]}"
        )
    return resp.text


async def route(
    waypoints: list[tuple[float, float]],
    profile: Profile = "trekking",
    alternative_idx: int = 0,
) -> dict:
    """Route between waypoints using BRouter. Returns route_id + metadata only.

    Fetches GPX and CSV in parallel. CSV provides per-segment way tags used to
    classify the route surface (cycleway / road / track / path / unknown) and
    compute a distance breakdown for "überwiegend Radwege"-style verification.

    Returns metadata + route_id + surface_breakdown. Full GPX stays in cache.
    """
    if len(waypoints) < 2:
        raise ValueError("Need at least 2 waypoints (start and end).")

    gpx_text, segments = await asyncio.gather(
        _fetch_gpx(waypoints, profile, alternative_idx),
        surface.fetch_segments(waypoints, profile, alternative_idx),
    )

    metadata = compute_metrics(gpx_text)
    metadata["profile"] = profile
    metadata["alternative_idx"] = alternative_idx
    metadata["waypoints"] = [{"lat": lat, "lon": lon} for lat, lon in waypoints]
    metadata["surface_breakdown"] = surface.breakdown(segments)

    projected = surface.project_to_gpx(segments, extract_latlon(gpx_text))
    groups = surface.group_lines(projected)
    route_id = state.store(gpx_text, metadata, surface_groups=groups)
    return state.get(route_id).metadata
