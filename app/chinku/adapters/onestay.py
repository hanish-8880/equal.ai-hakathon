"""1Stay hotels — LIVE adapter (open MCP endpoint, no key). SEARCH ONLY tonight.
Observed (DECISIONS Sun 03:15): POST https://mcp.stayker.com/mcp, Streamable HTTP, tools search_hotels {location, check_in,
check_out (YYYY-MM-DD), guests}, get_hotel_details, book_hotel (returns a secure checkout URL — never takes guest/payment
data). "Goa, India", 28→30 Nov, 2 guests → real hotels with total_stay / avg_nightly_rate in INR.
Chinku never books or pays here: the user completes any booking on 1Stay's own page."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.chinku.adapters import Offer, ProviderAdapter

URL = "https://mcp.stayker.com/mcp"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
IST = timezone(timedelta(hours=5, minutes=30))


def _parse(r: httpx.Response) -> dict[str, Any]:
    r.raise_for_status()
    t = r.text
    if "data:" in t:
        for line in t.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
    return r.json() if t.strip() else {}


def call_1stay(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=90) as c:
        r = c.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "chinku", "version": "0.1"}}}, headers=H)
        h = dict(H) | ({"Mcp-Session-Id": r.headers["mcp-session-id"]} if r.headers.get("mcp-session-id") else {})
        _parse(r)
        c.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        out = _parse(c.post(URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, headers=h))
    if out.get("error"):
        raise RuntimeError(f"1Stay: {out['error'].get('message')}")
    res = out.get("result") or {}
    if res.get("isError"):
        raise RuntimeError("1Stay: " + "".join(c.get("text", "") for c in res.get("content", []))[:300])
    if res.get("structuredContent"):
        return res["structuredContent"]
    txt = "".join(c.get("text", "") for c in res.get("content", []))
    try:
        return json.loads(txt)
    except ValueError:
        return {"text": txt}


def _addr(a: Any) -> str:
    if not isinstance(a, dict):
        return str(a or "")
    return ", ".join(x for x in (a.get("line1"), a.get("city")) if x and str(x).strip())


def to_offers(data: dict[str, Any], location: str) -> list[Offer]:
    now = datetime.now(IST).isoformat(timespec="minutes")
    out = []
    for h in data.get("results") or []:
        if not isinstance(h, dict) or not h.get("name"):
            continue
        total = h.get("total_stay")
        cur = h.get("currency") or "INR"
        out.append(Offer(provider="1Stay", category="hotel", item=h["name"] + (f" ({h['chain']})" if h.get("chain") else ""),
                         final_payable=float(total) if total is not None else None, currency=cur,
                         fees=float(h["booking_fee"]) if h.get("booking_fee") is not None and h.get("booking_fee_currency") == cur else None,
                         details={"hotel_id": h.get("hotel_id"), "nightly": h.get("avg_nightly_rate"), "nights": h.get("nights"),
                                  "address": _addr(h.get("address")), "location": location, "booking_type": h.get("booking_type"),
                                  "photo": h.get("photo")},
                         link=None, source="api", status="LIVE", fetched_at=now))
    return out


class OneStayHotels(ProviderAdapter):
    name, category, status = "1Stay", "hotel", "LIVE"

    def search(self, request: dict[str, Any]) -> list[Offer]:
        args = {"location": request["location"], "check_in": str(request["check_in"])[:10], "check_out": str(request["check_out"])[:10],
                "guests": max(1, min(10, int(request.get("guests") or 2)))}
        return to_offers(call_1stay("search_hotels", args), request["location"])
