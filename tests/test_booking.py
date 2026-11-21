import time
from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import ops
from app.jobs import store as jobs
from app.jobs.models import Job, Person, Window
from app.store.dynamo import MemoryStore, set_store, store

IST = timezone(timedelta(hours=5, minutes=30))
ME = "whatsapp:+919989683607"


@pytest.fixture(autouse=True)
def mem(monkeypatch):
    set_store(MemoryStore(path=None))
    monkeypatch.setattr(ops, "CARD", lambda to, text, card: CARDS.append(text))
    CARDS.clear()
    yield


CARDS: list[str] = []


def _w(hours_from_now: int, label: str) -> dict:
    s = (datetime.now(IST) + timedelta(hours=hours_from_now)).replace(minute=0, second=0, microsecond=0)
    return {"start": s.isoformat(), "end": (s + timedelta(hours=2)).isoformat(), "label": label}


def _task(job_id: str, windows: list[dict], chosen: str, quote: str = "4 to 7 is fine") -> dict:
    t = {"task_id": f"t_{job_id}", "user": ME, "inputs": {"job_id": job_id, "windows": windows, "plumber_name": "Hanish"},
         "steps": {"call_client": {"status": "done", "output": [{"status": "completed", "fields": {
             "chosen_slot": {"value": chosen, "status": "confirmed", "evidence_quote": quote},
             "flat": {"value": "Flat 304", "status": "confirmed", "evidence_quote": "304"}}}]}}}
    store().put("tasks", t["task_id"], t)
    return t


def _job(job_id: str, windows: list[dict]) -> Job:
    j = Job(job_id=job_id, plumber_id=ME, created_at=time.time())
    j.roles.site_contact = Person(name="Ramesh", phone="+919848012321")
    j.appointment.plumber_available = [Window(**w) for w in windows]
    return jobs.save(j)


def test_books_the_chosen_window_and_records_evidence():
    w1, w2 = _w(20, "Tomorrow 11–1"), _w(25, "Tomorrow 4–7")
    _job("J21", [w1, w2])
    r = ops.book_slot({"from_step": "call_client"}, {}, _task("J21", [w1, w2], w2["start"]), lambda m: None)
    j = jobs.get("J21")
    assert r["output"]["booked"] and j.appointment.state == "booked" and j.appointment.slot.label == "Tomorrow 4–7"
    assert j.appointment.requested_by_client.evidence_quote == "4 to 7 is fine" and j.flat.value == "Flat 304"
    assert r["output"]["fields"]["slot_start"]["value"] == w2["start"]  # feeds the confirmation-call schedule
    assert CARDS[-1].startswith("Booked ✅ J21")


def test_two_customers_cannot_take_one_slot():
    w1, w2 = _w(20, "Tomorrow 11–1"), _w(25, "Tomorrow 4–7")
    _job("J21", [w1, w2]); _job("J22", [w1, w2])
    ops.book_slot({"from_step": "call_client"}, {}, _task("J21", [w1, w2], w2["start"]), lambda m: None)
    r = ops.book_slot({"from_step": "call_client"}, {}, _task("J22", [w1, w2], w2["start"]), lambda m: None)
    j = jobs.get("J22")
    assert not r["output"]["booked"] and j.appointment.state == "proposed" and "taken" in j.next_action.blocked_by
    assert "Tomorrow 11–1" in CARDS[-1]  # offers the next free window
    assert [w["label"] for w in ops.free_windows(ME, [w1, w2])] == ["Tomorrow 11–1"]  # live availability


def test_customer_wants_other_time_keeps_visit_pending():
    w1 = _w(20, "Tomorrow 11–1")
    _job("J21", [w1])
    r = ops.book_slot({"from_step": "call_client"}, {}, _task("J21", [w1], "Sunday evening", "Sunday evening is better"), lambda m: None)
    j = jobs.get("J21")
    assert not r["output"]["booked"] and j.appointment.state == "pending" and j.next_action.owner == "plumber"


def test_past_windows_are_never_offered():
    assert ops.free_windows(ME, [_w(-5, "This morning")]) == []


# ---------------- parts: offers, ranking, dependency ----------------
from app.jobs import parts  # noqa: E402
from app.jobs.models import Dependency, Part  # noqa: E402


def F(v, st="confirmed", q="q"):
    return {"value": v, "status": st, "evidence_quote": q}


def test_offer_unknown_delivery_stays_unknown_and_condition_rule():
    o = parts.build_offer("Srinivas", "P1", 2, {"exact_model_available": F(True), "model_offered": F("ALD-0517"),
                                                  "unit_price": F(300), "price_condition": F("₹300 each only if 10 or more")}, "c1")
    assert o.delivery_charge is None and o.condition_met is False
    r = parts.rank_offers([o], 2)[0]
    assert r["unit_price"] is None and "not met" in r["price_note"] and r["total"] is None


def test_rank_exact_in_stock_beats_cheaper_different_model():
    a = parts.build_offer("Srinivas", "P1", 2, {"exact_model_available": F(False), "model_offered": F("ALD-0520"),
                                                  "exact_model_eta": F("tomorrow"), "unit_price": F(280)}, "c1")
    b = parts.build_offer("Ravi", "P1", 2, {"exact_model_available": F(True), "model_offered": F("ALD-0517"),
                                              "unit_price": F(350), "delivery_charge": F(0)}, "c2")
    rows = parts.rank_offers([a, b], 2)
    assert [r["shop"] for r in rows] == ["Ravi", "Srinivas"] and rows[0]["total"] == 700 and rows[1]["stock"] == "tomorrow"


def test_dependency_not_met_keeps_visit_pending_and_says_why():
    j = _job("J21", [])
    j.parts_needed.append(Part(part_id="P1", description="angle valve", model="ALD-0517", qty=2))
    j.dependencies.append(Dependency(part_id="P1", shop="Srinivas", exact_model=True, max_unit_price=500, then="today 4–6"))
    a = parts.build_offer("Srinivas", "P1", 2, {"exact_model_available": F(False), "model_offered": F("ALD-0520"),
                                                  "exact_model_eta": F("tomorrow"), "unit_price": F(280)}, "c1")
    state, msg = parts.check_dependencies(j, "P1", parts.rank_offers([a], 2))
    assert state == "not_met" and "ALD-0520" in msg and "tomorrow" in msg and j.appointment.state == "pending"
    assert j.next_action.owner == "plumber"


def test_dependency_met_moves_to_booking():
    j = _job("J21", [])
    j.parts_needed.append(Part(part_id="P1", description="angle valve", model="ALD-0517", qty=2))
    j.dependencies.append(Dependency(part_id="P1", shop="Srinivas", exact_model=True, max_unit_price=500, then="today 4–6"))
    a = parts.build_offer("Srinivas Hardware", "P1", 2, {"exact_model_available": F(True), "model_offered": F("ALD-0517"), "unit_price": F(450)}, "c1")
    state, msg = parts.check_dependencies(j, "P1", parts.rank_offers([a], 2))
    assert state == "met" and "today 4–6" in j.next_action.what
