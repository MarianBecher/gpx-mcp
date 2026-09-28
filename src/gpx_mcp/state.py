"""In-memory route cache. Tools return handles, GPX stays here."""
from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cached_property

from .metrics import cumulative_distances, elevations, parse_points

MAX_ENTRIES = 64


@dataclass
class CachedRoute:
    gpx: str
    metadata: dict
    # One entry per BRouter message row (or per trackpoint for reclassified GPX):
    # {dist_m, tags, node_tags, klass, paved, traffic, steps, ferry, cycle_route}
    segments: list[dict] = field(default_factory=list)
    # For each trackpoint the index into `segments` (empty if no surface info).
    point_seg: list[int] = field(default_factory=list)

    @cached_property
    def pts(self):
        return parse_points(self.gpx)

    @cached_property
    def cum(self) -> list[float]:
        return cumulative_distances(self.pts)

    @cached_property
    def elev(self) -> list[float]:
        return elevations(self.pts)

    @property
    def has_surface(self) -> bool:
        return bool(self.segments) and len(self.point_seg) == len(self.pts)

    def klasses(self) -> list[str]:
        """Per-trackpoint klass (`unknown` when no surface info)."""
        if not self.has_surface:
            return ["unknown"] * len(self.pts)
        return [self.segments[j]["klass"] for j in self.point_seg]


_routes: OrderedDict[str, CachedRoute] = OrderedDict()


def store(
    gpx: str,
    metadata: dict,
    segments: list[dict] | None = None,
    point_seg: list[int] | None = None,
) -> str:
    route_id = uuid.uuid4().hex[:12]
    _routes[route_id] = CachedRoute(
        gpx=gpx,
        metadata={**metadata, "route_id": route_id},
        segments=segments or [],
        point_seg=point_seg or [],
    )
    _routes.move_to_end(route_id)
    while len(_routes) > MAX_ENTRIES:
        _routes.popitem(last=False)
    return route_id


def get(route_id: str) -> CachedRoute:
    if route_id not in _routes:
        raise KeyError(
            f"Unknown route_id: {route_id!r}. Cache holds {len(_routes)} entries "
            f"(most recent: {list(_routes)[-3:]}). Use load_gpx to re-import a saved file."
        )
    _routes.move_to_end(route_id)
    return _routes[route_id]


def list_ids() -> list[str]:
    return list(_routes)


def summaries() -> list[dict]:
    """Compact one-line view of every cached route (for list_routes)."""
    out = []
    for rid, r in _routes.items():
        m = r.metadata
        out.append(
            {
                "route_id": rid,
                "distance_km": m.get("distance_km"),
                "ascent_m": m.get("ascent_m"),
                "profile": m.get("profile"),
                "source": m.get("source_path") or m.get("derived_from") or "brouter",
                "name": m.get("name"),
            }
        )
    return out
