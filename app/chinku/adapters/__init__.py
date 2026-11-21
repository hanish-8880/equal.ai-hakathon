"""Connected services layer (CLAUDE.md §6A): one adapter per provider, same shape; ranking is deterministic code.
"If they provide an API, it plugs in." Chinku never pays: book() only ever returns the provider's own booking link."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Priority = Literal["cheapest", "fastest", "earliest"]


class Offer(BaseModel):
    provider: str
    category: str
    item: str                                   # e.g. "HYD → BLR · IndiGo 6E 123 · non-stop"
    final_payable: float | None = None          # what the provider shows as the total (None = unknown, never 0)
    currency: str = "INR"
    fees: float | None = None                   # unknown → None (shown as "unknown")
    duration_min: int | None = None
    stops: int | None = None
    depart: str | None = None                   # local ISO time
    arrive: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    link: str | None = None                     # the provider's own page to book / pay
    match: Literal["exact", "similar"] = "exact"
    source: Literal["api", "call", "web", "demo"] = "api"
    status: Literal["LIVE", "DEMO"] = "LIVE"
    fetched_at: str = ""


class Recommendation(BaseModel):
    priority: Priority
    winner: Offer
    why: list[str]
    runner_up: Offer | None = None
    what_would_change: str | None = None
    table: list[Offer]
    excluded: list[dict[str, str]] = Field(default_factory=list)


class ProviderAdapter:
    name: str = ""
    category: str = ""
    status: str = "LIVE"

    def search(self, request: dict[str, Any]) -> list[Offer]:
        raise NotImplementedError

    def book(self, offer: Offer) -> dict[str, Any]:
        """Never pays. Returns the provider's own link; the user completes checkout there."""
        return {"action": "open_link", "url": offer.link, "paid": False}


def _money(v: float | None, cur: str) -> str:
    if v is None:
        return "unknown"
    return (f"₹{v:,.0f}" if cur == "INR" else f"{v:,.0f} {cur}")


def _hm(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _t(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0") if iso else "unknown"
    except ValueError:
        return iso or "unknown"


KEY = {
    "cheapest": lambda o: (o.final_payable is None, o.final_payable or 0, o.duration_min or 10**6),
    "fastest": lambda o: (o.duration_min is None, o.duration_min or 0, o.final_payable or 10**9),
    "earliest": lambda o: (o.arrive is None, o.arrive or "", o.final_payable or 10**9),
}


def rank(offers: list[Offer], priority: Priority, max_stops: int | None = None, arrive_by: str | None = None) -> Recommendation | None:
    """Deterministic ranking + reasons in words. Constraints that fail are excluded WITH the reason."""
    keep, excluded = [], []
    for o in offers:
        if max_stops is not None and o.stops is not None and o.stops > max_stops:
            excluded.append({"item": o.item, "why": f"{o.stops} stop(s) — you asked for at most {max_stops}"})
        elif arrive_by and o.arrive and o.arrive > arrive_by:
            excluded.append({"item": o.item, "why": f"arrives {_t(o.arrive)}, after {_t(arrive_by)}"})
        else:
            keep.append(o)
    if not keep:
        return None
    table = sorted(keep, key=KEY[priority])
    w, r = table[0], (table[1] if len(table) > 1 else None)
    cheapest = min((o for o in keep if o.final_payable is not None), key=lambda o: o.final_payable, default=None)
    fastest = min((o for o in keep if o.duration_min is not None), key=lambda o: o.duration_min, default=None)
    why = {"cheapest": [f"lowest price: {_money(w.final_payable, w.currency)}"],
           "fastest": [f"shortest trip: {_hm(w.duration_min)}"],
           "earliest": [f"arrives first: {_t(w.arrive)}"]}[priority]
    if w.stops == 0:
        why.append("non-stop")
    if priority != "cheapest" and cheapest and cheapest is not w and w.final_payable is not None:
        why.append(f"costs {_money(w.final_payable - cheapest.final_payable, w.currency)} more than the cheapest")
    if priority == "cheapest" and fastest and fastest is not w and w.duration_min and fastest.duration_min:
        why.append(f"{_hm(w.duration_min - fastest.duration_min)} longer than the fastest")
    change = None
    if priority == "cheapest" and fastest and fastest is not w:
        change = f"If speed matters more: {fastest.item} — {_hm(fastest.duration_min)} for {_money(fastest.final_payable, fastest.currency)}"
    elif priority != "cheapest" and cheapest and cheapest is not w:
        change = f"If price matters more: {cheapest.item} — {_money(cheapest.final_payable, cheapest.currency)}"
    return Recommendation(priority=priority, winner=w, why=why, runner_up=r, what_would_change=change, table=table, excluded=excluded)


def registry() -> dict[str, list[ProviderAdapter]]:
    from app.chinku.adapters.kiwi import KiwiFlights
    return {"flight": [KiwiFlights()]}
