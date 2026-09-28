"""Offline tests: metrics, climbs, classification, editing, sidecar round-trip."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from gpx_mcp import edit, gpx_utils, state
from gpx_mcp.metrics import compute_metrics, cumulative_distances, elevations, find_climbs, parse_points
from gpx_mcp.surface import breakdown, classify_tags, map_points_to_segments, segments_from_messages


def synthetic_track(n: int = 400, step_m: float = 25.0, profile=None) -> list[tuple[float, float, float]]:
    """Straight track heading east from Nürnberg; elevation from `profile(dist_m)`."""
    lat0, lon0 = 49.45, 11.08
    dlon = step_m / (111_320 * math.cos(math.radians(lat0)))
    pts = []
    for i in range(n):
        d = i * step_m
        ele = profile(d) if profile else 300.0
        pts.append((lat0, lon0 + i * dlon, ele))
    return pts


def hill(d: float) -> float:
    # flat 2 km, climb 80 m over 1 km (8 %), flat 1 km, descend 80 m over 2 km, rest flat
    if d < 2000:
        return 300.0
    if d < 3000:
        return 300.0 + (d - 2000) * 0.08
    if d < 4000:
        return 380.0
    if d < 6000:
        return 380.0 - (d - 4000) * 0.04
    return 300.0


@pytest.fixture(autouse=True)
def clear_cache():
    state._routes.clear()
    yield
    state._routes.clear()


def test_metrics_and_climbs():
    gpx = gpx_utils.build_gpx(synthetic_track(400, 25.0, hill), name="t")
    m = compute_metrics(gpx)
    assert 9.9 < m["distance_km"] < 10.1
    assert 75 <= m["ascent_m"] <= 85
    assert 75 <= m["descent_m"] <= 85
    assert 7.5 <= m["max_grade_percent"] <= 8.5
    assert m["n_climbs"] == 1
    c = m["climbs"][0]
    assert abs(c["start_km"] - 2.0) < 0.1 and abs(c["end_km"] - 3.0) < 0.1
    assert 75 <= c["gain_m"] <= 85 and 7.5 <= c["avg_grade_pct"] <= 8.5
    assert len(m["elevation_profile"]) == 30 and len(m["sampled_geometry"]) == 20
    assert isinstance(m["elevation_profile"][0], list)


def test_climb_tolerates_short_flat():
    def prof(d):
        if d < 1000:
            return 300.0
        if d < 1500:
            return 300 + (d - 1000) * 0.06
        if d < 1700:
            return 330.0  # 200 m flat inside the climb
        if d < 2200:
            return 330 + (d - 1700) * 0.06
        return 360.0
    pts = synthetic_track(200, 25.0, prof)
    parsed = parse_points(gpx_utils.build_gpx(pts))
    climbs = find_climbs(cumulative_distances(parsed), elevations(parsed))
    assert len(climbs) == 1
    assert 55 <= climbs[0]["gain_m"] <= 65


@pytest.mark.parametrize(
    "tags, klass, paved, traffic",
    [
        ({"highway": "cycleway", "surface": "asphalt"}, "cycleway", True, "none"),
        ({"highway": "path", "bicycle": "designated"}, "cycleway", None, "none"),
        ({"highway": "track", "tracktype": "grade2"}, "track", False, "none"),
        ({"highway": "track", "surface": "asphalt"}, "road", True, "none"),
        ({"highway": "primary"}, "road", True, "busy"),
        ({"highway": "primary", "cycleway:right": "lane"}, "road", True, "busy_with_lane"),
        ({"highway": "residential", "surface": "sett"}, "road", True, "quiet"),
        ({"highway": "steps"}, "path", None, "none"),
        ({"highway": "unclassified", "surface": "gravel"}, "road", False, "quiet"),
    ],
)
def test_classify(tags, klass, paved, traffic):
    c = classify_tags(tags)
    assert (c["klass"], c["paved"], c["traffic"]) == (klass, paved, traffic)
    assert c["steps"] == (tags.get("highway") == "steps")


def test_messages_breakdown_and_cycle_routes():
    header = ["Longitude", "Latitude", "Elevation", "Distance", "CostPerKm", "ElevCost", "TurnCost",
              "NodeCost", "InitialCost", "WayTags", "NodeTags", "Time", "Energy"]
    rows = [
        ["11080000", "49450000", "300", "500", "0", "0", "0", "0", "0", "highway=cycleway route_bicycle_rcn=yes", "", "0", "0"],
        ["11081000", "49450000", "300", "300", "0", "0", "0", "0", "0", "highway=primary", "barrier=bollard", "0", "0"],
        ["11082000", "49450000", "300", "200", "0", "0", "0", "0", "0", "highway=steps", "", "0", "0"],
    ]
    segs = segments_from_messages([header, *rows])
    assert len(segs) == 3 and segs[1]["node_tags"] == {"barrier": "bollard"}
    bd = breakdown(segs)
    assert bd["cycleway_pct"] == 50.0 and bd["busy_road_pct"] == 30.0
    assert bd["steps_count"] == 1 and bd["steps_m"] == 200
    assert bd["signed_cycle_route_pct"] == 50.0 and bd["cycle_route_network_pct"] == {"rcn": 50.0}


def test_map_points_to_segments_monotonic():
    pts = [(49.45, 11.08 + i * 0.0001) for i in range(50)]
    segs = [{"dist_m": 180}, {"dist_m": 180}]
    ps = map_points_to_segments(segs, pts)
    assert len(ps) == 50 and ps[0] == 0 and ps[-1] == 1 and ps == sorted(ps)


def _routed_fixture() -> str:
    """A cached route with synthetic per-point segments (as reclassify would produce)."""
    pts = synthetic_track(400, 25.0, hill)
    gpx = gpx_utils.build_gpx(pts, name="fixture")
    segments, point_seg = [], []
    parsed = parse_points(gpx)
    cum = cumulative_distances(parsed)
    for i in range(len(parsed)):
        tags = {"highway": "cycleway"} if i < 200 else {"highway": "secondary"}
        d = cum[i] - cum[i - 1] if i else 0.0
        if segments and segments[-1]["tags"] == tags:
            segments[-1]["dist_m"] += d
        else:
            seg = {"dist_m": d, "tags": tags, "node_tags": {}}
            seg.update(classify_tags(tags))
            segments.append(seg)
        point_seg.append(len(segments) - 1)
    meta = compute_metrics(gpx)
    meta["surface_breakdown"] = breakdown(segments)
    return state.store(gpx, meta, segments=segments, point_seg=point_seg)


def test_reverse_trim_concat():
    rid = _routed_fixture()
    orig = state.get(rid).metadata

    rev = edit.reverse(rid)
    assert abs(rev["distance_km"] - orig["distance_km"]) < 0.01
    assert rev["ascent_m"] == orig["descent_m"] and rev["descent_m"] == orig["ascent_m"]
    assert abs(rev["surface_breakdown"]["cycleway_pct"] - 50.0) < 1.5
    assert abs(rev["surface_breakdown"]["busy_road_pct"] - 50.0) < 1.5

    a = edit.trim(rid, 0, 5)
    b = edit.trim(rid, 5, orig["distance_km"])
    assert abs(a["distance_km"] - 5.0) < 0.05
    assert a["surface_breakdown"]["cycleway_pct"] > 98
    assert b["surface_breakdown"]["busy_road_pct"] > 98
    assert 75 <= a["ascent_m"] <= 85 and a["descent_m"] <= 45

    joined = edit.concat([a["route_id"], b["route_id"]])
    assert abs(joined["distance_km"] - orig["distance_km"]) < 0.05
    assert joined["join_gaps_m"] == [0]
    assert abs(joined["surface_breakdown"]["cycleway_pct"] - 50.0) < 1.5

    with pytest.raises(ValueError):
        edit.trim(rid, 6, 5)


def test_save_load_roundtrip_keeps_surface(tmp_path: Path):
    rid = _routed_fixture()
    wpts = [{"lat": 49.45, "lon": 11.09, "name": "Café Test", "type": "cafe"}]
    path = gpx_utils.save_gpx(rid, "round trip!", tmp_path, description="desc", waypoints=wpts)
    assert Path(path).name == "round_trip.gpx"
    assert gpx_utils.sidecar_path(Path(path)).is_file()
    assert "<wpt" in Path(path).read_text()

    loaded = gpx_utils.load_gpx(path)
    assert loaded["name"] == "round trip!"
    assert loaded["waypoints_in_file"][0]["name"] == "Café Test"
    assert abs(loaded["surface_breakdown"]["cycleway_pct"] - 50.0) < 1.5
    assert state.get(loaded["route_id"]).has_surface

    listed = gpx_utils.list_saved(tmp_path)
    assert listed[0]["name"] == "round_trip.gpx" and listed[0]["has_surface"]
    gpx_utils.delete_saved("round_trip", tmp_path)
    assert gpx_utils.list_saved(tmp_path) == []


def test_load_without_sidecar_hints(tmp_path: Path):
    p = tmp_path / "plain.gpx"
    p.write_text(gpx_utils.build_gpx(synthetic_track(50)))
    m = gpx_utils.load_gpx(str(p))
    assert m["surface_breakdown"] is None and "reclassify_surface" in m["hint"]
