"""Connected services (CLAUDE.md §6A): Kiwi.com mapping on a REAL saved response; ranking is code; Chinku never pays."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.chinku import adapters, tools
from app.chinku.adapters import Offer, kiwi

IST = timezone(timedelta(hours=5, minutes=30))
RAW = json.loads((Path(__file__).parent / "fixtures" / "kiwi_hyd_blr.json").read_text(encoding="utf-8"))


def test_kiwi_real_response_maps_to_offers():
    offers = kiwi.to_offers(RAW)
    assert len(offers) == 6
    o = offers[0]
    assert o.provider == "Kiwi.com" and o.status == "LIVE" and o.source == "api"
    assert o.final_payable == 66.0 and o.currency == "EUR" and o.duration_min == 285 and o.stops == 1
    assert o.depart == "2026-11-27T07:00:00" and o.arrive == "2026-11-27T11:45:00"
    assert o.link and o.link.startswith("https://kiwi.com/") and "via VTZ" in o.item and "Air India" in o.item
    assert o.details["flights"] and all(f.startswith(("AI", "6E", "QP", "IX", "SG", "UK")) or f for f in o.details["flights"])


def test_rank_cheapest_fastest_earliest_from_real_data():
    offers = kiwi.to_offers(RAW)
    c = adapters.rank(offers, "cheapest")
    f = adapters.rank(offers, "fastest")
    e = adapters.rank(offers, "earliest")
    assert c.winner.final_payable == 66.0 and "lowest price" in c.why[0]
    assert f.winner.duration_min == min(o.duration_min for o in offers) == 235 and "costs" in " ".join(f.why)
    assert e.winner.arrive == min(o.arrive for o in offers) == "2026-11-27T06:20:00"
    assert c.what_would_change and "speed" in c.what_would_change


def test_unknown_price_is_never_cheapest_and_constraints_exclude_with_reason():
    a = Offer(provider="A", category="flight", item="A", final_payable=None, duration_min=60, stops=0, arrive="2026-11-27T08:00:00")
    b = Offer(provider="B", category="flight", item="B", final_payable=5000, duration_min=90, stops=1, arrive="2026-11-27T09:00:00")
    d = Offer(provider="D", category="flight", item="D", final_payable=4000, duration_min=300, stops=2, arrive="2026-11-27T21:00:00")
    r = adapters.rank([a, b, d], "cheapest", max_stops=1, arrive_by="2026-11-27T20:00:00")
    assert r.winner.item == "B" and r.table[-1].item == "A"  # unknown price ranks last, never as ₹0
    assert r.excluded and r.excluded[0]["item"] == "D" and "stop" in r.excluded[0]["why"]


def test_book_never_pays():
    o = kiwi.to_offers(RAW)[0]
    out = kiwi.KiwiFlights().book(o)
    assert out == {"action": "open_link", "url": o.link, "paid": False}


def test_compare_tool_card_and_past_date(monkeypatch):
    monkeypatch.setattr(kiwi.KiwiFlights, "search", lambda self, req: kiwi.to_offers(RAW))
    cards = []
    ctx = {"task": type("T", (), {"task_id": "i_x"})(), "card": lambda t, c: cards.append((t, c))}
    nxt = (datetime.now(IST) + timedelta(days=5)).date().isoformat()
    out = tools.compare("u", {"category": "flight", "from": "Hyderabad", "to": "Bengaluru", "date": nxt, "adults": 1, "priority": "fastest"}, ctx)
    assert out["count"] == 6 and out["recommended"]["duration_min"] == 235
    c = cards[0][1]
    assert c["type"] == "compare" and c["winner"]["link"] and c["status"] == ["LIVE"] and "never pays" in cards[0][0]
    with pytest.raises(tools.ToolError, match="past"):
        tools.compare("u", {"category": "flight", "from": "HYD", "to": "BLR", "date": "2020-01-01"}, ctx)
    with pytest.raises(tools.ToolError, match="can't compare"):
        tools.compare("u", {"category": "rocket"}, ctx)


def test_one_provider_failing_is_reported_not_hidden(monkeypatch):
    def boom(self, req):
        raise RuntimeError("timeout")
    monkeypatch.setattr(kiwi.KiwiFlights, "search", boom)
    ctx = {"task": type("T", (), {"task_id": "i_x"})(), "card": lambda t, c: None}
    nxt = (datetime.now(IST) + timedelta(days=5)).date().isoformat()
    with pytest.raises(tools.ToolError, match="Kiwi.com: timeout"):
        tools.compare("u", {"category": "flight", "from": "HYD", "to": "BLR", "date": nxt}, ctx)
