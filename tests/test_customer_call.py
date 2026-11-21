"""A customer calls Chinku and books a visit: code maps the slot, locks it, never double-books, never books on a guess."""
import pytest

from app.brain.schemas import FactOut
from app.config import settings
from app.jobs import customer_call, ops, slots
from app.store.dynamo import MemoryStore, set_store

OUT: list[tuple[str, str]] = []
NOTES: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def mem(monkeypatch):
    set_store(MemoryStore(path=None))
    OUT.clear(); NOTES.clear()
    monkeypatch.setattr(ops, "CARD", lambda to, text, card: OUT.append((to, text)))
    monkeypatch.setattr(ops, "NOTIFY", lambda to, text, audio=None: NOTES.append((to, text)))
    monkeypatch.setattr(slots, "sync_soon", lambda: None)
    yield


def F(v, st="confirmed", q=None):
    return FactOut(value=v, status=st, evidence_quote=q or str(v))


def tomorrow_id() -> str:
    """Id of tomorrow's first working window (same numbering as handle())."""
    per_day = len([p for p in settings().work_hours.split(",") if p.strip()])
    return f"W{per_day + 1}"


def read(slot_id=None, st="confirmed", kind="customer", readback=True, pref=None):
    return customer_call.CustomerCallOut(caller_kind=kind, caller_name=F("Ramesh"), issue=F("kitchen tap leak"),
                                         site_address=F("Green Park, Kondapur"), flat=F("Flat 101, 1st floor"),
                                         access_notes=F("wife will open"), chosen_slot_id=F(slot_id, st) if slot_id else FactOut(),
                                         preferred_time_text=pref, readback_confirmed=readback)


CALL = {"from_number": "+919876500001", "call_id": "omni-1", "transcript": "user: tap leaking", "summary": "booking"}


def test_customer_call_books_and_locks_slot():
    job = customer_call.handle(CALL, read(tomorrow_id()))
    assert job.appointment.state == "booked" and job.appointment.confirmed_both
    assert job.roles.site_contact.phone == "+919876500001" and job.roles.site_contact.name == "Ramesh"
    assert ops.slot_taken(job.plumber_id, job.appointment.slot.start)
    assert any("New booking from a customer call" in t for _, t in OUT)
    assert job.appointment.slot.label not in [w["label"] for w in slots.open_slots(job.plumber_id)]  # no longer offered


def test_second_caller_cannot_take_the_same_slot():
    first = customer_call.handle(CALL, read(tomorrow_id()))
    second = customer_call.handle({**CALL, "from_number": "+919876500002", "call_id": "omni-2"}, read(tomorrow_id()))
    assert first.appointment.state == "booked"
    assert second.appointment.state == "pending" and second.appointment.slot is None
    assert "just taken" in second.next_action.blocked_by


def test_no_booking_on_a_guess():
    j1 = customer_call.handle(CALL, read(tomorrow_id(), st="tentative", readback=False))  # bare 'yes', never read back
    assert j1.appointment.state == "pending"
    j2 = customer_call.handle({**CALL, "call_id": "omni-3"}, read(None, pref="Sunday 8 pm"))  # time outside working hours
    assert j2.appointment.state == "pending" and j2.appointment.requested_by_client.value == "Sunday 8 pm"


def test_customer_whatsapp_only_if_joined():
    customer_call.handle(CALL, read(tomorrow_id()))
    assert NOTES == []  # +919876500001 never joined the sandbox → no fake WhatsApp
    from app.store.dynamo import store
    store().put("contacts", "wajoined:+919876500009", {"kind": "wa_joined"})
    customer_call.handle({**CALL, "from_number": "+919876500009", "call_id": "omni-9"}, read(f"W{int(tomorrow_id()[1:]) + 1}"))
    assert NOTES and "Your visit is booked" in NOTES[0][1]


def test_half_call_is_only_a_missed_enquiry():
    """Real case (Sat 16:51): caller said 'book an appointment… the application…' and the call dropped."""
    r = customer_call.CustomerCallOut(caller_kind="customer", issue=F("application", "tentative"))
    assert customer_call.handle(CALL, r) is None
    assert any("Missed enquiry" in t for _, t in OUT)


def test_plumber_call_is_not_a_customer_booking():
    assert customer_call.handle(CALL, read(tomorrow_id(), kind="plumber")) is None
