import pytest

from app.brain import understand as U
from app.brain.schemas import Action, FactOut, JobDraft, PersonOut, PlumberRoute, UnderstandResult, WindowOut
from app.jobs import assistant
from app.jobs import store as jobs
from app.store.dynamo import MemoryStore, set_store, store

ME = "whatsapp:+919989683607"


@pytest.fixture(autouse=True)
def mem():
    set_store(MemoryStore(path=None))
    yield


def F(v=None, st="confirmed", q="q"):
    return FactOut(value=v, status=st if v is not None else "not_mentioned", evidence_quote=q if v is not None else None)


def draft(phones, windows=True):
    return JobDraft(issue=F("Kitchen tap repair"), site_address=F("Green Park"), flat=F("Flat 304"), access_notes=F(None),
                    site_contact=PersonOut(name="Ramesh", phones=phones), billing_to=PersonOut(name="Sai Maintenance", email="bills@sai.example"),
                    approver=None, work_order=F("WO-117"),
                    plumber_windows=[WindowOut(start="2026-11-22T11:00:00+05:30", end="2026-11-22T13:00:00+05:30", label="Tomorrow 11–1", evidence_quote="11-1")] if windows else [])


@pytest.fixture
def brain(monkeypatch):
    state = {"route": UnderstandResult(actions=[Action(intent="create_job", job_ref="new", confidence=0.95)], readback="New job"),
             "draft": draft(["98480 12321"])}
    monkeypatch.setattr(assistant, "json_call", lambda model, system, user, schema, task_id=None, tag="", **kw: state["draft"])
    monkeypatch.setattr(U, "json_call", lambda model, system, user, schema, task_id=None, tag="", **kw: state["route"])
    out, cards = [], []
    monkeypatch.setattr(assistant, "CARD", lambda to, text, card: cards.append((text, card)))
    return state, out, cards


def test_job_ids_unique_and_start_at_21():
    a, b = jobs.new_job_id(), jobs.new_job_id()
    assert a == "J21" and b == "J22"


def test_create_job_one_number_sets_next_action_call(brain):
    state, out, cards = brain
    assistant.handle(ME, "Chinku, call this customer 98480 12321. Kitchen tap repair at Flat 304 ...", None, out.append)
    j = jobs.get("J21")
    assert j and j.roles.site_contact.phone == "+919848012321" and j.roles.work_order == "WO-117"
    assert j.issue.evidence_quote and j.appointment.plumber_available[0].label == "Tomorrow 11–1"
    assert j.next_action.owner == "chinku" and "book a visit" in j.next_action.what
    text, card = cards[-1]
    assert card["job_id"] == "J21" and card["site_contact"]["phone"].endswith("2321") and "******" in card["site_contact"]["phone"]
    assert j.timeline[0].kind == "job_created"


def test_create_job_two_numbers_asks_which_then_resolves(brain):
    state, out, cards = brain
    state["draft"] = draft(["98480 12321", "90000 55555"])
    assistant.handle(ME, "customer 98480 12321 or 90000 55555 ...", None, out.append)
    j = jobs.get("J21")
    assert j.roles.site_contact.phone is None and "Which number" in cards[-1][0]
    assistant.handle(ME, "call the second one", None, out.append)
    j = jobs.get("J21")
    assert j.roles.site_contact.phone == "+919000055555" and "book a visit" in j.next_action.what


def test_missing_windows_asks_only_for_windows(brain):
    state, out, cards = brain
    state["draft"] = draft(["98480 12321"], windows=False)
    assistant.handle(ME, "new job ...", None, out.append)
    assert "When are you free" in cards[-1][0]


def test_resolve_job_never_guesses_with_several_open(brain):
    state, out, cards = brain
    assistant.handle(ME, "job 1", None, out.append)
    assistant.handle(ME, "job 2", None, out.append)
    j, err = assistant.resolve_job(ME, PlumberRoute(intent="book_visit"))
    assert j is None and "Which job?" in err and "J21" in err and "J22" in err
    j, err = assistant.resolve_job(ME, PlumberRoute(intent="book_visit", job_ref="J22"))
    assert j.job_id == "J22" and err is None
    j, err = assistant.resolve_job(ME, PlumberRoute(intent="book_visit", job_ref="J99"))
    assert j is None and "couldn't find" in err


def test_other_intent_falls_through_to_v1_engine(brain, monkeypatch):
    state, out, cards = brain
    state["route"] = UnderstandResult(actions=[Action(intent="other", confidence=0.9)], readback="")
    seen = []
    monkeypatch.setattr(assistant.runner, "handle_message", lambda user, text, image, send, **kw: seen.append(text))
    assistant.handle(ME, "mixer warranty mail pettu", None, out.append)
    assert seen == ["mixer warranty mail pettu"]
