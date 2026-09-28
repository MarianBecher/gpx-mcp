"""Route editing on cached handles: reverse, trim, concat. Each returns a new route_id."""
from __future__ import annotations

from . import state
from .gpx_utils import build_gpx
from .metrics import compute_metrics, index_at_distance
from .surface import breakdown


def _points(cached: state.CachedRoute, i0: int = 0, i1: int | None = None):
    pts = cached.pts[i0 : (None if i1 is None else i1 + 1)]
    return [(p.latitude, p.longitude, p.elevation) for p in pts]


def _finish(points, segments, point_seg, derived_from: str, note: str, extra: dict | None = None) -> dict:
    gpx_text = build_gpx(points, name=derived_from)
    metadata = compute_metrics(gpx_text)
    metadata["derived_from"] = derived_from
    metadata["edit"] = note
    if segments and len(point_seg) == len(points):
        metadata["surface_breakdown"] = breakdown(segments)
    else:
        segments, point_seg = [], []
        metadata["surface_breakdown"] = None
    if extra:
        metadata.update(extra)
    rid = state.store(gpx_text, metadata, segments=segments, point_seg=point_seg)
    return state.get(rid).metadata


def _segments_for_range(cached: state.CachedRoute, i0: int, i1: int) -> tuple[list[dict], list[int]]:
    """Segments and remapped point_seg for trackpoints i0..i1 (inclusive).

    Segment distances are re-derived from the trackpoints so partial segments
    at the cut points don't over-count.
    """
    if not cached.has_surface:
        return [], []
    cum = cached.cum
    src_seg = cached.point_seg[i0 : i1 + 1]
    new_segments: list[dict] = []
    point_seg: list[int] = []
    last_src = None
    for k, sj in enumerate(src_seg):
        i = i0 + k
        d = cum[i] - cum[i - 1] if k > 0 else 0.0
        if sj != last_src:
            src = cached.segments[sj]
            new_segments.append({**src, "dist_m": d, "lat": cached.pts[i].latitude, "lon": cached.pts[i].longitude})
            last_src = sj
        else:
            new_segments[-1]["dist_m"] += d
        point_seg.append(len(new_segments) - 1)
    for s in new_segments:
        s["dist_m"] = round(s["dist_m"])
    return new_segments, point_seg


def _nearest_index_at_distance(cum: list[float], target_m: float) -> int:
    i = index_at_distance(cum, target_m)
    if i > 0 and target_m - cum[i - 1] < cum[i] - target_m:
        i -= 1
    return i


def reverse(route_id: str) -> dict:
    cached = state.get(route_id)
    n = len(cached.pts)
    segs, pseg = _segments_for_range(cached, 0, n - 1)
    points = list(reversed(_points(cached)))
    if segs:
        # Reversed point k corresponds to original point n-1-k; its segment's
        # distance now belongs to the following (reversed) point.
        rev_pseg = list(reversed(pseg))
        rev_segs: list[dict] = []
        remap: dict[int, int] = {}
        new_pseg: list[int] = []
        for k, sj in enumerate(rev_pseg):
            if sj not in remap:
                remap[sj] = len(rev_segs)
                rev_segs.append({**segs[sj], "dist_m": 0.0})
            new_pseg.append(remap[sj])
        cum = cached.cum
        for k in range(1, n):
            i = n - 1 - k  # original index of reversed point k
            rev_segs[new_pseg[k]]["dist_m"] += cum[i + 1] - cum[i]
        for s in rev_segs:
            s["dist_m"] = round(s["dist_m"])
        segs, pseg = rev_segs, new_pseg
    return _finish(points, segs, pseg, route_id, "reverse")


def trim(route_id: str, from_km: float, to_km: float) -> dict:
    cached = state.get(route_id)
    total_km = cached.cum[-1] / 1000
    if from_km < 0 or to_km > total_km + 0.01 or from_km >= to_km:
        raise ValueError(f"Need 0 <= from_km < to_km <= {total_km:.2f}")
    # Snap both cuts the same way so trim(a, b) and trim(b, c) share the cut point.
    i0 = _nearest_index_at_distance(cached.cum, from_km * 1000)
    i1 = _nearest_index_at_distance(cached.cum, to_km * 1000)
    if i1 - i0 < 1:
        raise ValueError("Trim range too short (fewer than 2 trackpoints).")
    segs, pseg = _segments_for_range(cached, i0, i1)
    return _finish(
        _points(cached, i0, i1), segs, pseg, route_id,
        f"trim {from_km}-{to_km} km",
        {"trim_from_km": from_km, "trim_to_km": to_km},
    )


def concat(route_ids: list[str]) -> dict:
    if len(route_ids) < 2:
        raise ValueError("Need at least 2 route_ids to concatenate.")
    points: list[tuple] = []
    segments: list[dict] = []
    point_seg: list[int] = []
    all_surface = True
    gaps_m: list[float] = []
    from .metrics import haversine_m

    for rid in route_ids:
        c = state.get(rid)
        segs, pseg = _segments_for_range(c, 0, len(c.pts) - 1)
        pts = _points(c)
        if points:
            gaps_m.append(round(haversine_m(points[-1][0], points[-1][1], pts[0][0], pts[0][1])))
        offset = len(segments)
        points.extend(pts)
        if segs:
            segments.extend(segs)
            point_seg.extend(j + offset for j in pseg)
        else:
            all_surface = False
    if not all_surface:
        segments, point_seg = [], []
    return _finish(
        points, segments, point_seg, "+".join(route_ids), "concat",
        {"join_gaps_m": gaps_m, "hint": None if all(g < 100 for g in gaps_m) else "Large gap between parts — route the missing piece first."},
    )
