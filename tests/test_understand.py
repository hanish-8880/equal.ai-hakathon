import pytest

from app.brain import understand as U
from app.brain.schemas import Action, FactOut, JobDraft, PersonOut, UnderstandResult, WindowOut
from app.jobs import assistant
from app.jobs import store as jobs
from app.store.dynamo import MemoryStore, set_store

ME = "whatsapp:+919989683607"


@pytest.fixture(autouse=True)
def mem():
    set_store(MemoryStore(path=None))
    yield


def R(*actions, q=None, rb="readback"):
    return UnderstandResult(actions=list(actions), clarifying_question=q, readback=rb)


@pytest.mark.parametrize("res,expect", [
    (R(Action(intent="create_job", job_ref="new", confidence=0.95)), "act"),
    (R(Action(intent="create_job", job_ref="new", confidence=0.6)), "ask"),            # low confidence
    (R(Action(intent="book_visit", job_ref="J21", confidence=0.9, missing=["slot"])), "ask"),  # missing field
    (R(Action(intent="book_visit", job_ref="ambiguous", confidence=0.9)), "ask"),      # never guess a job
    (R(Action(intent="book_visit", job_ref="J21", confidence=0.95)), "confirm"),       # consequential → readback + yes
    (R(Action(intent="status_query", job_ref="J21", confidence=0.95), q="Which job?"), "ask"),
    (R(Action(intent="small_talk", confidence=0.3)), "act"),                          # no state change anyway
])
def test_gate(res, expect):
    assert U.gate(res) == expect


@pytest.fixture
def wired(monkeypatch):
    st = {"res": None}
    draft = JobDraft(issue=FactOut(value="Kitchen tap", status="confirmed", evidence_quote="Kitchen tap"),
                     site_address=FactOut(), flat=FactOut(value="Flat 304", status="confirmed", evidence_quote="Flat 304"),
                     access_notes=FactOut(), site_contact=PersonOut(phones=["98480 12321"]), work_order=FactOut(),
                     plumber_windows=[WindowOut(start="2026-11-22T16:00:00+05:30", end="2026-11-22T19:00:00+05:30", label="Tomorrow 4–7", evidence_quote="after 4")])
    monkeypatch.setattr(U, "json_call", lambda *a, **k: st["res"])
    monkeypatch.setattr(assistant, "json_call", lambda *a, **k: draft)
    cards = []
    monkeypatch.setattr(assistant, "CARD", lambda to, text, card: cards.append((text, card)))
    return st, cards


def test_consequential_waits_for_yes_then_starts_booking_call(wired, monkeypatch):
    from app.brain.schemas import CallBrief
    from app.engine import steps
    st, cards = wired
    dispatched = []
    monkeypatch.setattr(steps, "DISPATCH", lambda to, ctx: dispatched.append((to, ctx)) or {"requestId": "sim-1"})
    monkeypatch.setattr(steps.brief_writer, "write_brief", lambda step, *a, **k: CallBrief(
        on_behalf_of="Hanish", callee_role="customer", language="English", max_minutes=3, opening_line="AI for Hanish",
        goal=step["goal"], must_collect=[], closing_line="bye", call_script="script"))
    st["res"] = R(Action(intent="create_job", job_ref="new", confidence=0.95))
    out = []
    assistant.handle(ME, "new job", None, out.append)
    st["res"] = R(Action(intent="book_visit", job_ref="J21", confidence=0.95), rb="J21 · call customer · tomorrow 4–7")
    assistant.handle(ME, "J21 customer ki call chesi book chey", None, out.append)
    assert out[-1].startswith("J21 · call customer") and "shall I go ahead" in out[-1] and dispatched == []  # waits for yes
    assistant.handle(ME, "avunu", None, out.append)
    assert len(dispatched) == 1 and dispatched[0][0] == "+919848012321"
    assert "Calling" in cards[-1][0] and "Tomorrow 4–7" in cards[-1][0]
    assert jobs.get("J21").appointment.state == "proposed"


def test_ask_changes_nothing(wired):
    st, cards = wired
    st["res"] = R(Action(intent="create_job", job_ref="new", confidence=0.5), q="Is this a new job?")
    out = []
    assistant.handle(ME, "mumble", None, out.append)
    assert out == ["Is this a new job?"] and jobs.open_jobs(ME) == []


def test_correction_updates_only_that_field(wired):
    st, cards = wired
    st["res"] = R(Action(intent="create_job", job_ref="new", confidence=0.95))
    assistant.handle(ME, "new job", None, lambda m: None)
    before = jobs.get("J21")
    st["res"] = R(Action(intent="correct_detail", job_ref="J21", confidence=0.9,
                         entities={"field": "flat", "old_value": "Flat 304", "new_value": "Flat 310"}), rb="J21 · flat → Flat 310")
    assistant.handle(ME, "kaadu, Flat 310", None, lambda m: None)
    after = jobs.get("J21")
    assert after.flat.value == "Flat 310" and after.issue == before.issue and after.roles == before.roles
    assert cards[-1][0].startswith("Updated J21: flat → Flat 310")
    assert any(e.kind == "understood" for e in after.timeline)


def test_multi_intent_runs_in_order(wired):
    st, cards = wired
    st["res"] = R(Action(intent="create_job", job_ref="new", confidence=0.95))
    assistant.handle(ME, "new job", None, lambda m: None)
    st["res"] = R(Action(intent="correct_detail", job_ref="J21", confidence=0.9, entities={"field": "flat", "new_value": "Flat 9"}),
                  Action(intent="status_query", job_ref="J21", confidence=0.9))
    assistant.handle(ME, "flat 9, and status?", None, lambda m: None)
    assert cards[-2][0].startswith("Updated J21") and cards[-1][0].startswith("J21 ·")
