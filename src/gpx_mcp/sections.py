"""Section-level description of a cached route: what am I riding on between km A and km B?"""
from __future__ import annotations

from . import geocode, state
from .metrics import find_climbs, index_at_distance, interpolate_at_distance, section_stats
from .routing import hazards

MAX_PLACE_LOOKUPS = 6


def _run_key(seg: dict) -> tuple:
    t = seg.get("tags", {})
    return (t.get("highway", "?"), seg["klass"], seg["paved"], seg["traffic"], tuple(seg.get("cycle_route") or []))


def surface_runs(cached: state.CachedRoute, i0: int, i1: int, min_run_m: float) -> list[dict]:
    """Consecutive trackpoints with the same way character → runs with km bounds.

    Runs shorter than min_run_m are merged into their predecessor.
    """
    if not cached.has_surface:
        return []
    cum = cached.cum
    runs: list[dict] = []
    for i in range(i0, i1 + 1):
        seg = cached.segments[cached.point_seg[i]]
        key = _run_key(seg)
        if runs and runs[-1]["_key"] == key:
            runs[-1]["_end"] = i
            continue
        t = seg.get("tags", {})
        runs.append(
            {
                "_key": key, "_start": i, "_end": i,
                "highway": t.get("highway"),
                "klass": seg["klass"],
                "paved": seg["paved"],
                "surface": t.get("surface"),
                "smoothness": t.get("smoothness"),
                "traffic": seg["traffic"],
                "cycle_route": seg.get("cycle_route") or None,
            }
        )
    merged: list[dict] = []
    for r in runs:
        length = cum[r["_end"]] - cum[r["_start"]]
        if merged and length < min_run_m:
            merged[-1]["_end"] = r["_end"]
            continue
        merged.append(r)
    out = []
    for r in merged:
        s, e = r["_start"], r["_end"]
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        item = {
            "from_km": round(cum[s] / 1000, 2),
            "to_km": round(cum[e] / 1000, 2),
            "length_m": round(cum[e] - cum[s]),
            **item,
        }
        if e > s:
            de = cached.elev[e] - cached.elev[s]
            item["avg_grade_pct"] = round(de / (cum[e] - cum[s]) * 100, 1) if cum[e] > cum[s] else 0.0
        out.append(item)
    return out


async def describe(
    route_id: str,
    from_km: float | None = None,
    to_km: float | None = None,
    min_run_m: float = 150.0,
    with_places: bool = True,
) -> dict:
    cached = state.get(route_id)
    cum, elev = cached.cum, cached.elev
    total_km = cum[-1] / 1000
    a = 0.0 if from_km is None else max(0.0, from_km)
    b = total_km if to_km is None else min(total_km, to_km)
    if a >= b:
        raise ValueError(f"Need from_km < to_km within 0..{total_km:.2f}")
    i0 = index_at_distance(cum, a * 1000)
    i1 = index_at_distance(cum, b * 1000)
    if i0 > 0 and cum[i0] > a * 1000:
        i0 -= 1

    stats = section_stats(cum, elev, i0, i1)
    climbs = [c for c in find_climbs(cum, elev) if c["end_km"] > a and c["start_km"] < b]
    runs = surface_runs(cached, i0, i1, min_run_m)
    hz = [h for h in hazards(cached.segments) if a <= h["km"] <= b] if cached.has_surface else []

    places: list[dict] = []
    if with_places:
        n_lookups = min(MAX_PLACE_LOOKUPS, max(2, int((b - a) / 5) + 1))
        for k in range(n_lookups):
            km = a + (b - a) * k / (n_lookups - 1)
            p = interpolate_at_distance(cached.pts, cum, elev, km * 1000)
            try:
                rg = await geocode.reverse(p["lat"], p["lon"], zoom=16)
            except Exception as exc:  # noqa: BLE001 — a failed lookup must not kill the description
                rg = {"error": type(exc).__name__}
            places.append({"km": round(km, 1), **{k2: v for k2, v in rg.items() if k2 in ("road", "place", "error")}})

    return {
        "route_id": route_id,
        "from_km": round(a, 2),
        "to_km": round(b, 2),
        **stats,
        "start": interpolate_at_distance(cached.pts, cum, elev, a * 1000),
        "end": interpolate_at_distance(cached.pts, cum, elev, b * 1000),
        "climbs": climbs,
        "hazards": hz,
        "places": places,
        "runs": runs,
        "note": None if cached.has_surface else "No surface info for this route (imported GPX without sidecar). Run reclassify_surface first.",
    }
