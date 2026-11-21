"""Rides — DEMO adapters for Ola, Uber, Rapido (CLAUDE.md §6A: no public consumer booking API for us).
Distance is REAL (OpenStreetMap geocoding, straight line × 1.35 road factor); fares and pickup times are DEMO numbers from
a published-style formula (base + per km), labelled "Demo data" everywhere. The button opens the provider's own app/site
with pickup and drop filled in where they support it. Chinku never books or pays a ride."""
import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.chinku.adapters import Offer, ProviderAdapter

IST = timezone(timedelta(hours=5, minutes=30))
# provider → vehicle → (base ₹, per km ₹, min fare ₹)   DEMO rates
RATES = {
    "Rapido": {"bike": (15, 7.0, 25), "auto": (25, 12.0, 35)},
    "Ola": {"auto": (30, 14.0, 40), "cab": (55, 15.0, 90)},
    "Uber": {"auto": (28, 13.5, 38), "cab": (50, 16.0, 85)},
}
SPEED = {"bike": 24, "auto": 20, "cab": 19}  # km/h average in Hyderabad traffic (demo assumption)


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h)) * 1.35


def where(place: str) -> tuple[float, float]:
    from app.actions import maps
    g = maps.geocode(f"{place}, Hyderabad") or maps.geocode(place)
    if not g:
        raise ValueError(f"I couldn't find “{place}” on the map.")
    return g[0], g[1]


def links(provider: str, a: tuple[float, float], b: tuple[float, float]) -> str:
    if provider == "Uber":
        return (f"https://m.uber.com/ul/?action=setPickup&pickup[latitude]={a[0]}&pickup[longitude]={a[1]}"
                f"&dropoff[latitude]={b[0]}&dropoff[longitude]={b[1]}")
    if provider == "Ola":
        return f"https://book.olacabs.com/?pickup_lat={a[0]}&pickup_lng={a[1]}&drop_lat={b[0]}&drop_lng={b[1]}"
    return "https://www.rapido.bike/"


class DemoRides(ProviderAdapter):
    category, status = "ride", "DEMO"

    def __init__(self, provider: str):
        self.name = provider

    def search(self, request: dict[str, Any]) -> list[Offer]:
        a, b = request.get("_from_ll") or where(request["from"]), request.get("_to_ll") or where(request["to"])
        dist = max(0.5, km(a, b))
        now = request.get("_now") or datetime.now(IST)
        want = (request.get("vehicle") or "").lower()
        out = []
        for vehicle, (base, per_km, minimum) in RATES[self.name].items():
            if want and want != vehicle:
                continue
            h = int(hashlib.sha1(f"{self.name}{vehicle}{now:%Y%m%d%H}".encode()).hexdigest(), 16)
            pickup = 2 + h % 7                                    # 2–8 min, stable within the hour (demo)
            fare = round(max(minimum, base + per_km * dist))
            ride_min = round(dist / SPEED[vehicle] * 60)
            arrive = now + timedelta(minutes=pickup + ride_min)
            out.append(Offer(provider=self.name, category="ride", item=f"{self.name} {vehicle.title()} · pickup {pickup} min",
                             final_payable=float(fare), currency="INR", fees=None, duration_min=pickup + ride_min, stops=None,
                             depart=(now + timedelta(minutes=pickup)).isoformat(timespec="minutes"), arrive=arrive.isoformat(timespec="minutes"),
                             details={"vehicle": vehicle, "pickup_min": pickup, "ride_min": ride_min, "distance_km": round(dist, 1)},
                             link=links(self.name, a, b), source="demo", status="DEMO",
                             fetched_at=now.isoformat(timespec="minutes")))
        return out
