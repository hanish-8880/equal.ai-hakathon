"""Kiwi.com flights — LIVE adapter over its public MCP server (https://mcp.kiwi.com, Streamable HTTP, no auth).
Observed (DECISIONS Sun 01:20): tool `search-flight`, dates dd/mm/yyyy, city names resolved by Kiwi, result has price,
totalDurationSeconds, outbound{departureTime, arrivalTime, stops, route, segments[carrierName, flightNumber]}, bookingUrl.
Chinku never pays: the booking link opens Kiwi.com, where the user checks out themselves."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.chinku.adapters import Offer, ProviderAdapter

URL = "https://mcp.kiwi.com"
IST = timezone(timedelta(hours=5, minutes=30))
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _parse(r: httpx.Response) -> dict[str, Any]:
    if r.status_code >= 400:
        raise RuntimeError(f"Kiwi.com HTTP {r.status_code}: {r.text[:200]}")
    datas = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
    return json.loads(datas[-1] if datas else (r.text or "{}"))


def call_kiwi(arguments: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=90) as c:
        r = c.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "chinku", "version": "0.1"}}}, headers=H)
        h = dict(H) | ({"Mcp-Session-Id": r.headers["mcp-session-id"]} if r.headers.get("mcp-session-id") else {})
        _parse(r)
        c.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        out = _parse(c.post(URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": "search-flight", "arguments": arguments}}, headers=h))
    if out.get("error"):
        raise RuntimeError(f"Kiwi.com: {out['error'].get('message')}")
    res = out.get("result") or {}
    if res.get("isError"):
        raise RuntimeError("Kiwi.com: " + "".join(c.get("text", "") for c in res.get("content", []))[:300])
    if res.get("structuredContent"):
        return res["structuredContent"]
    return json.loads("".join(c.get("text", "") for c in res.get("content", [])) or "{}")


def to_offers(data: dict[str, Any]) -> list[Offer]:
    cur = data.get("currency") or "INR"
    now = datetime.now(IST).isoformat(timespec="minutes")
    offers = []
    for it in data.get("itineraries") or []:
        ob = it.get("outbound") or {}
        segs = ob.get("segments") or []
        airlines = sorted({s.get("carrierName") or s.get("carrier") for s in segs if s.get("carrierName") or s.get("carrier")})
        flights = [f"{s.get('carrier', '')}{str(s.get('flightNumber', '')).removeprefix(s.get('carrier', '') or '')}" for s in segs]
        stops = ob.get("stops")
        via = ob.get("route", [])[1:-1]
        label = " → ".join([ob.get("from") or (ob.get("route") or ["?"])[0], ob.get("to") or (ob.get("route") or ["?"])[-1]])
        stop_txt = "non-stop" if stops == 0 else f"{stops} stop{'s' if (stops or 0) > 1 else ''} via {', '.join(via)}" if stops else "stops unknown"
        dur = it.get("totalDurationSeconds") or ob.get("durationSeconds")
        offers.append(Offer(
            provider="Kiwi.com", category="flight", item=f"{label} · {' + '.join(airlines) or 'airline ?'} · {stop_txt}",
            final_payable=float(it["price"]) if it.get("price") is not None else None, currency=cur,
            duration_min=round(dur / 60) if dur else None, stops=stops, depart=ob.get("departureTime"), arrive=ob.get("arrivalTime"),
            details={"airlines": airlines, "flights": [f for f in flights if f], "route": ob.get("route"), "baggage": it.get("baggage"),
                     "cabin": ob.get("cabinClass"), "price_shown": it.get("priceFormatted")},
            link=it.get("bookingUrl"), source="api", status="LIVE", fetched_at=now))
    return offers


class KiwiFlights(ProviderAdapter):
    name, category, status = "Kiwi.com", "flight", "LIVE"

    def search(self, request: dict[str, Any]) -> list[Offer]:
        d = datetime.fromisoformat(str(request["date"])[:10])
        args = {"flyFrom": request["from"], "flyTo": request["to"], "departureDate": d.strftime("%d/%m/%Y"),
                "adults": int(request.get("adults") or 1), "currency": request.get("currency") or "INR", "locale": "en",
                "sort": {"cheapest": "price", "fastest": "duration", "earliest": "date"}.get(request.get("priority", "cheapest"), "price")}
        if request.get("return_date"):
            args["returnDate"] = datetime.fromisoformat(str(request["return_date"])[:10]).strftime("%d/%m/%Y")
        if request.get("max_stops") is not None:
            args["max_sector_stopovers"] = int(request["max_stops"])
        return to_offers(call_kiwi(args))
