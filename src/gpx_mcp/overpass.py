"""Overpass access with mirror rotation, retry and a small in-memory cache.

The public overpass-api.de instance is frequently overloaded (504 / 429).
Every Overpass query in this package goes through `query()` so the fallback
logic lives in one place.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time

import httpx

from .http import client

_DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

ENDPOINTS: tuple[str, ...] = tuple(
    e.strip()
    for e in os.environ.get("GPX_MCP_OVERPASS_URLS", ",".join(_DEFAULT_ENDPOINTS)).split(",")
    if e.strip()
)

_RETRY_STATUS = {429, 502, 503, 504}
_CACHE_TTL_S = 15 * 60
_CACHE_MAX = 128
_cache: dict[str, tuple[float, dict]] = {}


class OverpassError(RuntimeError):
    pass


def _cache_key(ql: str) -> str:
    return hashlib.sha1(ql.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if time.monotonic() - ts > _CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return data


def _cache_put(key: str, data: dict) -> None:
    if len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic(), data)


async def query(ql: str, *, timeout_s: int = 25, attempts_per_endpoint: int = 2) -> dict:
    """Run an Overpass QL query; `[out:json][timeout:N]` is prepended automatically.

    Tries each configured endpoint in turn, with a short backoff on 429/5xx.
    Results are cached for 15 minutes keyed on the query text.
    """
    full = f"[out:json][timeout:{timeout_s}];{ql}"
    key = _cache_key(full)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    http_timeout = httpx.Timeout(timeout_s + 10.0, connect=10.0)
    errors: list[str] = []
    for endpoint in ENDPOINTS:
        for attempt in range(attempts_per_endpoint):
            try:
                resp = await client().post(endpoint, data={"data": full}, timeout=http_timeout)
            except httpx.HTTPError as exc:
                errors.append(f"{endpoint}: {type(exc).__name__}")
                break  # network trouble with this mirror; move on
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{endpoint}: non-JSON response")
                    break
                if "remark" in data and not data.get("elements"):
                    errors.append(f"{endpoint}: {data['remark'][:120]}")
                    break
                _cache_put(key, data)
                return data
            errors.append(f"{endpoint}: HTTP {resp.status_code}")
            if resp.status_code not in _RETRY_STATUS:
                break
            await asyncio.sleep(1.5 * (attempt + 1))
    raise OverpassError(
        "All Overpass endpoints failed: " + "; ".join(errors)
        + ". Try again in a minute or narrow the query (smaller radius / shorter route)."
    )


def element_point(el: dict) -> tuple[float | None, float | None]:
    """(lat, lon) of a node, or the `center` of a way/relation queried with `out center`."""
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def around_clause(radius_m: int, coords: list[tuple[float, float]]) -> str:
    """Overpass `(around:R,lat,lon,lat,lon,...)` clause for a point chain."""
    flat = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in coords)
    return f"(around:{radius_m},{flat})"
