"""Way classification from OSM tags (BRouter WayTags or Overpass way tags).

Every routed segment gets:
  klass   cycleway | road | track | path | unknown      (what kind of way)
  paved   True | False | None                           (from `surface`, else inferred)
  traffic none | quiet | busy_with_lane | busy          (motor traffic exposure)
  steps   bool
  ferry   bool
"""
from __future__ import annotations

from typing import Literal

from .metrics import haversine_m

Klass = Literal["cycleway", "road", "track", "path", "unknown"]
ALL_KLASSES: tuple[Klass, ...] = ("cycleway", "road", "track", "path", "unknown")

_BUSY_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
}
_QUIET_HIGHWAYS = {
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service", "road",
}
_ROAD_HIGHWAYS = _BUSY_HIGHWAYS | _QUIET_HIGHWAYS

_PAVED_SURFACES = {
    "asphalt", "paved", "concrete", "concrete:plates", "concrete:lanes", "paving_stones",
    "sett", "cobblestone", "unhewn_cobblestone", "metal", "wood", "chipseal", "bricks",
}
_UNPAVED_SURFACES = {
    "unpaved", "gravel", "fine_gravel", "compacted", "dirt", "earth", "ground", "grass",
    "sand", "mud", "pebblestone", "woodchips", "rock", "grass_paver", "stepping_stones", "shells",
}
_CYCLE_LANE_VALUES = {"lane", "track", "opposite_lane", "opposite_track", "share_busway", "shared_lane"}


def parse_tags(way_tags: str) -> dict[str, str]:
    """`"highway=cycleway surface=asphalt"` → dict."""
    out: dict[str, str] = {}
    for tok in way_tags.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _klass(t: dict[str, str]) -> Klass:
    highway = t.get("highway", "")
    if highway == "cycleway":
        return "cycleway"
    if t.get("bicycle") == "designated" and highway in {"path", "footway"}:
        return "cycleway"
    if highway in {"path", "footway", "pedestrian", "steps", "bridleway"}:
        return "path"
    if highway == "track":
        return "road" if t.get("surface", "") in _PAVED_SURFACES else "track"
    if highway in _ROAD_HIGHWAYS:
        return "road"
    return "unknown"


def _paved(t: dict[str, str], klass: Klass) -> bool | None:
    surface = t.get("surface", "")
    if surface:
        base = surface.split(";")[0].split(":")[0] if surface not in _PAVED_SURFACES else surface
        if surface in _PAVED_SURFACES or base in _PAVED_SURFACES:
            return True
        if surface in _UNPAVED_SURFACES or base in _UNPAVED_SURFACES:
            return False
        return None
    tracktype = t.get("tracktype", "")
    if tracktype:
        return tracktype == "grade1"
    highway = t.get("highway", "")
    if highway in _ROAD_HIGHWAYS or highway == "cycleway":
        return True  # roads and cycleways without a surface tag are almost always sealed
    if highway == "track":
        return False
    return None


def _traffic(t: dict[str, str]) -> str:
    highway = t.get("highway", "")
    if highway in _BUSY_HIGHWAYS:
        lane_vals = {
            t.get("cycleway", ""), t.get("cycleway:both", ""),
            t.get("cycleway:right", ""), t.get("cycleway:left", ""),
        }
        if lane_vals & _CYCLE_LANE_VALUES or t.get("sidewalk:bicycle") == "yes":
            return "busy_with_lane"
        return "busy"
    if highway in _QUIET_HIGHWAYS:
        return "quiet"
    return "none"


_CYCLE_ROUTE_KEYS = ("route_bicycle_icn", "route_bicycle_ncn", "route_bicycle_rcn", "route_bicycle_lcn")


def cycle_route_networks(t: dict[str, str]) -> list[str]:
    """Signed cycle-route networks this way is part of (BRouter merges relations in)."""
    return [k.rsplit("_", 1)[1] for k in _CYCLE_ROUTE_KEYS if t.get(k) == "yes"]


def classify_tags(t: dict[str, str]) -> dict:
    """Full classification dict for a tag dict."""
    if not t:
        return {"klass": "unknown", "paved": None, "traffic": "none", "steps": False, "ferry": False, "cycle_route": []}
    klass = _klass(t)
    return {
        "klass": klass,
        "paved": _paved(t, klass),
        "traffic": _traffic(t),
        "steps": t.get("highway") == "steps",
        "ferry": t.get("route") == "ferry",
        "cycle_route": cycle_route_networks(t),
    }


def classify(way_tags: str) -> Klass:
    """Coarse class for a BRouter WayTags string (kept for the enrich path)."""
    if not way_tags or way_tags == "-":
        return "unknown"
    return _klass(parse_tags(way_tags))


def segments_from_messages(messages: list[list[str]]) -> list[dict]:
    """BRouter GeoJSON `messages` (header row + rows) → segment dicts.

    Each row describes the way segment ending at (lon, lat), with `Distance`
    metres from the previous message point. `NodeTags` describe the node at
    the end of the segment (barriers, crossings, ...).
    """
    if not messages:
        return []
    header = messages[0]
    col = {name: i for i, name in enumerate(header)}
    out: list[dict] = []
    for row in messages[1:]:
        if len(row) < len(header):
            continue
        tags = parse_tags(row[col["WayTags"]]) if row[col["WayTags"]] not in ("", "-") else {}
        node_tags = parse_tags(row[col["NodeTags"]]) if col.get("NodeTags") is not None and row[col["NodeTags"]] else {}
        seg = {
            "lat": int(row[col["Latitude"]]) / 1e6,
            "lon": int(row[col["Longitude"]]) / 1e6,
            "dist_m": int(row[col["Distance"]] or 0),
            "tags": tags,
            "node_tags": node_tags,
        }
        seg.update(classify_tags(tags))
        out.append(seg)
    return out


def map_points_to_segments(
    segments: list[dict], gpx_points: list[tuple[float, float]]
) -> list[int]:
    """For each GPX trackpoint, the index of the segment it belongs to.

    Alignment is by cumulative distance (BRouter's GPX has ~4x more points than
    message rows); scaled so both chains end together.
    """
    if not segments or not gpx_points:
        return []
    seg_cum: list[float] = []
    t = 0.0
    for s in segments:
        t += float(s["dist_m"])
        seg_cum.append(t)
    seg_total = seg_cum[-1]

    gpx_cum = [0.0]
    for i in range(1, len(gpx_points)):
        gpx_cum.append(
            gpx_cum[-1]
            + haversine_m(gpx_points[i - 1][0], gpx_points[i - 1][1], gpx_points[i][0], gpx_points[i][1])
        )
    gpx_total = gpx_cum[-1]
    if seg_total <= 0 or gpx_total <= 0:
        return [0] * len(gpx_points)

    scale = seg_total / gpx_total
    out: list[int] = []
    j = 0
    last_j = len(segments) - 1
    for i in range(len(gpx_points)):
        target = gpx_cum[i] * scale
        while j < last_j and seg_cum[j] < target:
            j += 1
        out.append(j)
    return out


def group_lines(gpx_points: list[tuple[float, float]], klasses: list[str]) -> list[dict]:
    """Consecutive same-class points → [{klass, coords:[[lon,lat],...]}] for the viewer.

    Adjacent groups share their boundary point so the rendered line stays continuous.
    """
    if not gpx_points:
        return []
    groups: list[dict] = []
    cur = klasses[0]
    coords: list[list[float]] = [[gpx_points[0][1], gpx_points[0][0]]]
    for (lat, lon), k in zip(gpx_points[1:], klasses[1:]):
        if k != cur:
            coords.append([lon, lat])
            groups.append({"klass": cur, "coords": coords})
            cur = k
            coords = [[lon, lat]]
        else:
            coords.append([lon, lat])
    groups.append({"klass": cur, "coords": coords})
    return groups


def breakdown(segments: list[dict]) -> dict:
    """Distance shares (percent) plus absolute counters for the annoying stuff."""
    total = 0.0
    by_klass = {k: 0.0 for k in ALL_KLASSES}
    paved = unpaved = surf_unknown = 0.0
    busy = busy_lane = quiet = 0.0
    steps_m = ferry_m = 0.0
    steps_runs = 0
    prev_steps = False
    on_route = 0.0
    by_network = {"icn": 0.0, "ncn": 0.0, "rcn": 0.0, "lcn": 0.0}
    for s in segments:
        d = float(s.get("dist_m", 0))
        total += d
        nets = s.get("cycle_route") or []
        if nets:
            on_route += d
            for n in nets:
                by_network[n] += d
        by_klass[s["klass"]] += d
        if s["paved"] is True:
            paved += d
        elif s["paved"] is False:
            unpaved += d
        else:
            surf_unknown += d
        if s["traffic"] == "busy":
            busy += d
        elif s["traffic"] == "busy_with_lane":
            busy_lane += d
        elif s["traffic"] == "quiet":
            quiet += d
        if s["steps"]:
            steps_m += d
            if not prev_steps:
                steps_runs += 1
        prev_steps = s["steps"]
        if s["ferry"]:
            ferry_m += d

    def pct(x: float) -> float:
        return round(x / total * 100, 1) if total > 0 else 0.0

    return {
        **{f"{k}_pct": pct(v) for k, v in by_klass.items()},
        "paved_pct": pct(paved),
        "unpaved_pct": pct(unpaved),
        "surface_unknown_pct": pct(surf_unknown),
        "busy_road_pct": pct(busy),
        "busy_road_with_lane_pct": pct(busy_lane),
        "quiet_road_pct": pct(quiet),
        "steps_count": steps_runs,
        "steps_m": round(steps_m),
        "ferry_m": round(ferry_m),
        "signed_cycle_route_pct": pct(on_route),
        "cycle_route_network_pct": {k: pct(v) for k, v in by_network.items() if v > 0},
    }
