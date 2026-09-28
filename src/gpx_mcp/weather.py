"""Weather and elevation via Open-Meteo (free, no API key, ~10k calls/day fair use)."""
from __future__ import annotations

from .http import client

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

_HOURLY = [
    "temperature_2m", "precipitation_probability", "precipitation",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "weather_code",
]
_WMO = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail",
}
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(deg: float) -> str:
    return _COMPASS[int((deg + 22.5) // 45) % 8]


async def forecast(lat: float, lon: float, date: str, from_hour: int = 6, to_hour: int = 20) -> dict:
    """Hourly forecast for one day (YYYY-MM-DD, up to 16 days ahead) at a point.

    Wind direction is meteorological (where the wind comes FROM): 270 = west wind.
    """
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": ",".join(_HOURLY),
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
        "start_date": date,
        "end_date": date,
        "timezone": "auto",
        "wind_speed_unit": "kmh",
    }
    resp = await client().get(FORECAST_URL, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"Open-Meteo error {resp.status_code}: {resp.text[:200]}")
    d = resp.json()
    h = d.get("hourly", {})
    times = h.get("time", [])
    hours = []
    for i, t in enumerate(times):
        hour = int(t[11:13])
        if hour < from_hour or hour > to_hour:
            continue
        wd = h["wind_direction_10m"][i]
        hours.append(
            {
                "hour": hour,
                "temp_c": h["temperature_2m"][i],
                "rain_prob_pct": h["precipitation_probability"][i],
                "rain_mm": h["precipitation"][i],
                "wind_kmh": h["wind_speed_10m"][i],
                "gusts_kmh": h["wind_gusts_10m"][i],
                "wind_from": compass(wd) if wd is not None else None,
                "wind_from_deg": wd,
                "sky": _WMO.get(h["weather_code"][i], str(h["weather_code"][i])),
            }
        )
    daily = d.get("daily", {})
    return {
        "lat": lat, "lon": lon, "date": date, "timezone": d.get("timezone"),
        "sunrise": (daily.get("sunrise") or [None])[0],
        "sunset": (daily.get("sunset") or [None])[0],
        "temp_min_c": (daily.get("temperature_2m_min") or [None])[0],
        "temp_max_c": (daily.get("temperature_2m_max") or [None])[0],
        "rain_sum_mm": (daily.get("precipitation_sum") or [None])[0],
        "wind_max_kmh": (daily.get("wind_speed_10m_max") or [None])[0],
        "hours": hours,
    }


async def elevation(points: list[tuple[float, float]]) -> list[dict]:
    """Elevation (m, Copernicus DEM 90 m) for up to 100 points."""
    pts = points[:100]
    params = {
        "latitude": ",".join(f"{lat:.5f}" for lat, _ in pts),
        "longitude": ",".join(f"{lon:.5f}" for _, lon in pts),
    }
    resp = await client().get(ELEVATION_URL, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"Open-Meteo elevation error {resp.status_code}: {resp.text[:200]}")
    eles = resp.json().get("elevation", [])
    return [{"lat": lat, "lon": lon, "ele_m": e} for (lat, lon), e in zip(pts, eles)]
