"""J3 complaint follow-through, J7 scam check, J2 booking rule, reminder calls — code rules tested for real (model mocked)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.chinku import executor, tools
from app.chinku.models import Plan, Step, Task
from app.store.dynamo import MemoryStore, set_store, store

IST = timezone(timedelta(hours=5, minutes=30))
ME = "whatsapp:+919989683607"
CARDS, OUT = [], []


@pytest.fixture(autouse=True)
def mem():
    set_store(MemoryStore(path=None))
    CARDS.clear(); OUT.clear()
    yield


def ctx(tid="i_t"):
    return {"task": Task(task_id=tid, user=ME, request="x", plan=Plan(understood="x"), created_at=0, updated_at=0),
            "card": lambda t, c: CARDS.append((t, c)), "step_id": "s1", "send": OUT.append}


@pytest.mark.parametrize("brand,url,ok", [
    ("Bajaj", "https://www.bajajelectricals.com/contact-us", True), ("Prestige", "https://www.ttkprestige.com/support", True),
    ("Bajaj", "https://www.justdial.com/bajaj-service-center", False), ("Philips", "https://www.consumercomplaints.in/philips", False),
    ("Bajaj", None, False)])
def test_official_is_decided_by_domain(brand, url, ok):
    assert tools.is_official(brand, url) is ok


def test_scam_two_red_flags_never_pass_as_genuine(monkeypatch):
    class R:
        output_text = json.dumps({"verdict": "Looks genuine", "reasons": ["looks fine"], "what_to_do": [], "official_sources": []})
    monkeypatch.setattr("app.brain.client.oai", lambda: type("O", (), {"responses": type("X", (), {"create": staticmethod(lambda **k: R())})()})())
    msg = "Dear customer your electricity will be disconnected tonight. Update KYC immediately, share OTP at bit.ly/xyz"
    out = tools.scam_check(ME, {"text": msg}, ctx())
    assert out["verdict"] == "Unsure" and len(out["red_flags"]) >= 2
    assert CARDS[-1][1]["type"] == "scam" and "cybercrime.gov.in" in " ".join(out["what_to_do"])


def test_promise_fires_once_and_escalates():
    due = (datetime.now(IST) + timedelta(hours=2)).isoformat()
    p = tools.promise(ME, {"who": "Bajaj support", "what": "technician visit", "by_when_iso": due, "ticket": "BJ-55120", "demo_minutes": 0.01}, ctx())
    rec = store().get("contacts", f"prom:{p['pid']}")
    fired = []
    for _ in range(2):
        for x in tools.due_promises(rec["due"] + 5):
            tools.fire_promise(x, lambda u, t, c: fired.append(c))
    assert len(fired) == 1 and fired[0]["type"] == "followup" and fired[0]["ticket"] == "BJ-55120"
    out = tools.promise_update(ME, {"resolved": False}, ctx())
    assert out["resolved"] is False and any("1915" in s for s in out["ladder"])
    assert store().get("contacts", f"prom:{p['pid']}")["status"] == "not_kept"


def test_booked_only_when_a_booking_field_is_confirmed():
    base = {"callee": {"name": "Sri Sai Clinic"}, "status": "completed", "to_number": "+919800000001", "transcript": "t"}
    tent = executor.call_summary(base | {"extracted": {"fields": [{"name": "confirm_booking", "value": "yes", "status": "tentative"}]}})
    conf = executor.call_summary(base | {"extracted": {"fields": [{"name": "confirm_booking", "value": "Sun 6 PM, Dr Rao",
                                                                   "status": "confirmed", "evidence_quote": "Yes, 6 PM is booked"}]}})
    no = executor.call_summary(base | {"extracted": {"fields": [{"name": "slot_available", "value": "no", "status": "confirmed"}]}})
    assert (tent["booked"], conf["booked"], no["booked"]) == (False, True, False)


def test_auto_promise_needs_a_real_quote(monkeypatch):
    t = ctx()["task"]
    when = (datetime.now(IST) + timedelta(hours=3)).isoformat()
    monkeypatch.setattr(tools, "json_call", lambda *a, **k: executor._Proms(promises=[
        executor._Prom(who="support", what="callback", by_when_iso=when, ticket="T1", evidence_quote="we will call you back by 4 PM"),
        executor._Prom(who="support", what="refund", by_when_iso=when, evidence_quote="refund in five days guaranteed")]))
    c = {"status": "completed", "who": "Bajaj support", "transcript": "user: ok, we will call you back by 4 PM today. Ticket T1."}
    executor.auto_promises(t, c, lambda u, x, cd: CARDS.append((x, cd)))
    proms = store().scan("contacts", kind="promise")
    assert len(proms) == 1 and proms[0]["what"] == "callback"  # the refund quote isn't in the transcript → not tracked


def test_reminder_call_needs_approval_and_validates_number(monkeypatch):
    p = Plan(understood="r", confidence=0.95, steps=[Step(id="s1", tool="remind", inputs={
        "text": "Doctor at 6 PM", "when_iso": (datetime.now(IST) + timedelta(hours=4)).isoformat(),
        "for_person": "Mummy", "call_phone": "9800000001", "call_language": "Telugu"})])
    monkeypatch.setattr("app.chinku.planner.plan", lambda *a, **k: p)
    executor.handle(ME, "remind mummy by call", [], OUT.append, lambda t, c: CARDS.append((t, c)))
    assert store().scan("contacts", kind="reminder") == [] and any("Needs you" in t for t, _ in CARDS)
    with pytest.raises(tools.ToolError, match="not a valid"):
        tools.remind(ME, {"text": "x", "when_iso": (datetime.now(IST) + timedelta(hours=1)).isoformat(), "call_phone": "12345"}, ctx())


def test_trip_sorted_by_rating_and_only_real_numbers_callable(monkeypatch):
    class R:
        output_text = json.dumps({"title": "Araku 2 days", "itinerary": [{"day": "Day 1", "plan": ["Borra Caves"]}],
                                  "hotels": [{"name": "A", "rating": 3.9, "phone": "9876543210"}, {"name": "B", "rating": 4.6, "phone": "+91 98490 11111"},
                                             {"name": "C", "rating": None, "phone": None}],
                                  "restaurants": [{"name": "R1", "rating": 4.1, "phone": "0891-2345678"}], "sources": ["https://x"]})
    monkeypatch.setattr("app.brain.client.oai", lambda: type("O", (), {"responses": type("X", (), {"create": staticmethod(lambda **k: R())})()})())
    monkeypatch.setattr(tools, "map_", lambda *a, **k: {})
    out = tools.trip_plan(ME, {"destination": "Araku", "days": 2, "people": 4}, ctx())
    assert [h["name"] for h in out["hotels"]] == ["B", "A", "C"]
    assert [h["callable"] for h in out["hotels"]] == [True, True, False]
    assert out["restaurants"][0]["callable"] is False  # landline → shown, but the mobile-only call engine won't dial it
