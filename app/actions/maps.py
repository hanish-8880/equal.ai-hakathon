"""OpenStreetMap geocoding (Nominatim) for the Instint map card. Free, no key; usage policy: 1 req/s, cached 24 h."""
import time
from typing import Any

import httpx

UA = {"User-Agent": "instint-hackathon-prototype/0.1"}  # generic client name, no personal data
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def geocode(place: str) -> tuple[float, float, str] | None:
    def go():
        r = httpx.get("https://nominatim.openstreetmap.org/search", params={"q": place, "format": "json", "limit": 1, "countrycodes": "in"},
                      headers=UA, timeout=20)
        r.raise_for_status()
        js = r.json()
        return (float(js[0]["lat"]), float(js[0]["lon"]), js[0]["display_name"]) if js else None
    return _cached(f"geo:{place.lower()}", 86400, go)
