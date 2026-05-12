"""In-memory route cache. Tools return handles, GPX stays here."""
from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_ENTRIES = 64


@dataclass
class CachedRoute:
    gpx: str
    metadata: dict
    surface_groups: list[dict] = field(default_factory=list)


_routes: OrderedDict[str, CachedRoute] = OrderedDict()


def store(gpx: str, metadata: dict, surface_groups: list[dict] | None = None) -> str:
    route_id = uuid.uuid4().hex[:12]
    _routes[route_id] = CachedRoute(
        gpx=gpx,
        metadata={**metadata, "route_id": route_id},
        surface_groups=surface_groups or [],
    )
    _routes.move_to_end(route_id)
    while len(_routes) > MAX_ENTRIES:
        _routes.popitem(last=False)
    return route_id


def get(route_id: str) -> CachedRoute:
    if route_id not in _routes:
        raise KeyError(f"Unknown route_id: {route_id!r}. Cache holds {len(_routes)} entries (most recent: {list(_routes)[-3:]}).")
    _routes.move_to_end(route_id)
    return _routes[route_id]


def list_ids() -> list[str]:
    return list(_routes)
