"""Nearby plumbing / hardware shops from OpenStreetMap (Nominatim geocoding + Overpass). Free, no key.
VERIFIED (DECISIONS Sat 20:20): Hyderabad coverage = names + locations, usually NO phone numbers, Overpass ~30 s.
Shops found here are SHOWN (address, distance, map link); Chinku calls one only after the plumber picks it and gives/has its number."""
import math
import time
from typing import Any

import httpx

UA = {"User-Agent": "chinku-hackathon-prototype/0.1"}  # generic client name, no personal data
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
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


def _km(a: float, b: float, c: float, d: float) -> float:
    return 6371 * 2 * math.asin(math.sqrt(math.sin(math.radians(c - a) / 2) ** 2
                                          + math.cos(math.radians(a)) * math.cos(math.radians(c)) * math.sin(math.radians(d - b) / 2) ** 2))


def nearby_shops(lat: float, lon: float, radius_m: int = 4000, limit: int = 8) -> list[dict[str, Any]]:
    q = (f'[out:json][timeout:30];(nwr["shop"~"hardware|doityourself|bathroom_furnishing|trade"](around:{radius_m},{lat},{lon});'
         f'nwr["craft"="plumber"](around:{radius_m},{lat},{lon}););out center 60;')

    def go():
        last = None
        for url in OVERPASS:
            try:
                r = httpx.post(url, data={"data": q}, headers=UA, timeout=45)
                r.raise_for_status()
                return r.json().get("elements", [])
            except httpx.HTTPError as e:
                last = e
        raise RuntimeError(f"overpass failed: {last}")
    rows = []
    for e in _cached(f"shops:{round(lat, 3)},{round(lon, 3)},{radius_m}", 3600, go):
        t = e.get("tags", {})
        la = e.get("lat") or (e.get("center") or {}).get("lat")
        lo = e.get("lon") or (e.get("center") or {}).get("lon")
        if not t.get("name") or la is None:
            continue
        addr = ", ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb"), t.get("addr:city")) if x)
        rows.append({"name": t["name"], "kind": (t.get("shop") or t.get("craft") or "").replace("_", " "),
                     "distance_km": round(_km(lat, lon, la, lo), 1), "phone": t.get("phone") or t.get("contact:phone"),
                     "address": addr or None, "lat": la, "lon": lo,
                     "map_url": f"https://www.google.com/maps/search/?api=1&query={la},{lo}",
                     "source": "OpenStreetMap"})
    rows.sort(key=lambda r: r["distance_km"])
    return rows[:limit]
