"""Cycling duration estimation from a route's elevation profile.

Piecewise grade-adjusted speed model. Calibrated for a trekking bike + moderately
fit rider; the caller can scale via `avg_speed_kmh` (flat-ground speed).
"""
from __future__ import annotations

from . import state

# Descent caps: realistic ceilings on a loaded trekking bike (safety, surface quality).
_DESCENT_CAP_KMH = 35.0
_STEEP_DESCENT_CAP_KMH = 40.0


def speed_for_grade(grade_pct: float, flat_speed_kmh: float) -> float:
    """Piecewise grade → speed table. Bergauf nonlinear, bergab gedeckelt."""
    if grade_pct >= 10:
        return flat_speed_kmh * 0.22
    if grade_pct >= 7:
        return flat_speed_kmh * 0.32
    if grade_pct >= 5:
        return flat_speed_kmh * 0.45
    if grade_pct >= 3:
        return flat_speed_kmh * 0.65
    if grade_pct >= 1:
        return flat_speed_kmh * 0.85
    if grade_pct >= -1:
        return flat_speed_kmh
    if grade_pct >= -3:
        return flat_speed_kmh * 1.20
    if grade_pct >= -5:
        return min(_DESCENT_CAP_KMH, flat_speed_kmh * 1.35)
    return min(_STEEP_DESCENT_CAP_KMH, flat_speed_kmh * 1.5)


def _fmt_hm(minutes: float) -> str:
    h = int(minutes // 60)
    m = int(round(minutes - h * 60))
    if m == 60:
        h += 1
        m = 0
    return f"{h}h{m:02d}"


def estimate(
    route_id: str,
    avg_speed_kmh: float = 18.0,
    break_minutes_per_hour: float = 5.0,
) -> dict:
    """Estimate moving time + total time (incl. breaks) for a cached route.

    Args:
        route_id: handle from `route` or `load_gpx`.
        avg_speed_kmh: flat-ground cruising speed. Defaults to 18 km/h (trekking).
        break_minutes_per_hour: break time added per hour of moving time.

    Returns moving/total minutes, human-readable strings, and effective speed.
    """
    cached = state.get(route_id)
    pts = cached.pts
    if len(pts) < 2:
        return {
            "route_id": route_id,
            "distance_km": 0.0,
            "moving_time_minutes": 0,
            "total_time_minutes": 0,
            "moving_time_human": "0h00",
            "total_time_human": "0h00",
            "effective_speed_kmh": 0.0,
            "params": {
                "avg_speed_kmh": avg_speed_kmh,
                "break_minutes_per_hour": break_minutes_per_hour,
            },
        }

    cum = cached.cum
    total_m = cum[-1]

    moving_seconds = 0.0
    last_known = pts[0].elevation if pts[0].elevation is not None else 0.0
    for i in range(1, len(pts)):
        d = cum[i] - cum[i - 1]
        if d <= 0:
            continue
        e_prev = last_known
        if pts[i].elevation is not None:
            last_known = pts[i].elevation
        grade = ((last_known - e_prev) / d) * 100
        v_kmh = speed_for_grade(grade, avg_speed_kmh)
        moving_seconds += d / (v_kmh * 1000 / 3600)

    moving_minutes = moving_seconds / 60
    break_minutes = (moving_minutes / 60) * break_minutes_per_hour
    total_minutes = moving_minutes + break_minutes
    effective_speed = (total_m / 1000) / (moving_minutes / 60) if moving_minutes > 0 else 0.0

    return {
        "route_id": route_id,
        "distance_km": round(total_m / 1000, 2),
        "moving_time_minutes": round(moving_minutes),
        "total_time_minutes": round(total_minutes),
        "moving_time_human": _fmt_hm(moving_minutes),
        "total_time_human": _fmt_hm(total_minutes),
        "effective_speed_kmh": round(effective_speed, 1),
        "params": {
            "avg_speed_kmh": avg_speed_kmh,
            "break_minutes_per_hour": break_minutes_per_hour,
        },
    }
