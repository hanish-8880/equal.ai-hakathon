"""Chinku: planner → approval gate → tools → results (model calls mocked; code rules tested for real)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.chinku import executor, memory, tools
from app.chinku.models import Plan, Step
from app.store.dynamo import MemoryStore, set_store, store

IST = timezone(timedelta(hours=5, minutes=30))
ME = "whatsapp:+919989683607"
OUT: list[str] = []
CARDS: list[tuple[str, dict | None]] = []
SENT: list[tuple] = []


@pytest.fixture(autouse=True)
def mem(monkeypatch, tmp_path):
    set_store(MemoryStore(path=None))
    OUT.clear(); CARDS.clear(); SENT.clear()
    from app.actions import email as email_action
    monkeypatch.setattr(email_action, "send", lambda to, subject, body, image=None, filename=None: SENT.append((to, subject, body, filename)) or to)
    yield


def send(t): OUT.append(t)
def card(t, c): CARDS.append((t, c))


def photo(tmp_path):
    p = tmp_path / "page.jpg"
    p.write_bytes(b"\xff\xd8fakejpeg")
    return [{"id": "a1", "name": "page.jpg", "mime": "image/jpeg", "url": "https://x/media/page.jpg", "path": str(p)}]


TOMORROW_9 = (datetime.now(IST) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
J1 = Plan(understood="Transcribe the page · email you a copy · remind you tomorrow 9 AM", confidence=0.95, steps=[
    Step(id="s1", tool="transcribe_image", inputs={"attachment": "a1"}, output_name="transcript"),
    Step(id="s2", tool="draft_email", inputs={"to": "me", "mode": "exact_copy", "content": "{s1.text}", "title": "{s1.title}", "attach": True},
         depends_on=["s1"], output_name="draft"),
    Step(id="s3", tool="send_email", inputs={"draft": "{s2}"}, depends_on=["s2"], needs_approval=True),
    Step(id="s4", tool="remind", inputs={"text": "Review the transcript", "when_iso": TOMORROW_9})])


def test_j1_photo_to_email_waits_for_yes_then_sends_exact_text_once(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings(), "smtp_user", "me@example.com")
    monkeypatch.setattr("app.chinku.planner.plan", lambda *a, **k: J1)
    page = "Milk 2 L\nRice 5 kg — ₹350\n[unclear] by Friday"
    monkeypatch.setattr(tools, "json_call", lambda *a, **k: tools.Transcript(text=page, kind="handwritten", title="Shopping list"))
    executor.handle(ME, "Transcribe this, email me a copy, and remind me tomorrow at 9 to review it.", photo(tmp_path), send, card)
    assert SENT == []  # nothing leaves before approval
    assert any("Needs you" in t and "me@example.com" in t for t, _ in CARDS)
    t = executor.load(store().get("contacts", f"iwait:{ME}")["task_id"])
    assert t.status == "waiting" and t.steps["s3"].preview["body"].count(page) == 1  # exact transcript, not rewritten
    assert t.steps["s1"].output["unclear_count"] == 1
    executor.handle(ME, "yes", [], send, card)
    assert len(SENT) == 1 and SENT[0][0] == "me@example.com" and page in SENT[0][2] and SENT[0][3] == "page.jpg"
    rem = [r for r in store().scan("contacts", kind="reminder")]
    assert len(rem) == 1 and rem[0]["text"] == "Review the transcript"
    t = executor.load(t.task_id)
    assert t.status == "done"
    executor.handle(ME, "yes", [], send, card)  # a second 'yes' never sends again
    assert len(SENT) == 1


def test_no_means_nothing_is_sent(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings(), "smtp_user", "me@example.com")
    monkeypatch.setattr("app.chinku.planner.plan", lambda *a, **k: J1)
    monkeypatch.setattr(tools, "json_call", lambda *a, **k: tools.Transcript(text="hello", title="x"))
    executor.handle(ME, "transcribe and email me", photo(tmp_path), send, card)
    executor.handle(ME, "vaddu", [], send, card)
    assert SENT == [] and any("Nothing left the app" in o for o in OUT)


def test_send_email_needs_approval_even_if_the_plan_forgot(monkeypatch, tmp_path):
    p = Plan(understood="x", confidence=0.9, steps=[Step(id="s1", tool="send_email", needs_approval=False,
                                                         inputs={"draft": {"to": "a@b.com", "subject": "s", "body": "b"}})])
    monkeypatch.setattr("app.chinku.planner.plan", lambda *a, **k: p)
    executor.handle(ME, "send it", [], send, card)
    assert SENT == [] and any("Needs you" in t for t, _ in CARDS)


def test_unknown_recipient_asks_instead_of_guessing(monkeypatch):
    with pytest.raises(tools.ToolError, match="Suresh"):
        tools.resolve_email(ME, "Suresh")
    memory.remember(ME, "Suresh email", "suresh@example.com", "contact")
    assert tools.resolve_email(ME, "Suresh") == "suresh@example.com"


@pytest.mark.parametrize("k,v", [("bank otp", "482913"), ("card", "4111 1111 1111 1111"), ("aadhaar", "1234 5678 9012"),
                                 ("my password", "hunter2"), ("pan", "ABCDE1234F")])
def test_memory_never_stores_secrets(k, v):
    assert memory.remember(ME, k, v)["saved"] is False and memory.recall(ME) == []


def test_memory_remember_recall_forget():
    assert memory.remember(ME, "Mummy phone", "+919800000001", "contact")["saved"]
    assert memory.get(ME, "mummy phone") == "+919800000001"
    assert memory.forget(ME, "Mummy phone") and memory.recall(ME) == []


def test_reminder_in_the_past_is_refused_and_repeat_reschedules():
    with pytest.raises(tools.ToolError, match="already past"):
        tools.parse_when((datetime.now(IST) - timedelta(hours=2)).isoformat())
    ctx = {"task": type("T", (), {"task_id": "i_x"})(), "card": card}
    r = tools.remind(ME, {"text": "BP tablet", "when_iso": (datetime.now(IST) + timedelta(minutes=5)).isoformat(), "repeat": "daily"}, ctx)
    due = store().get("contacts", f"rem:{r['rid']}")["due"]
    fired = []
    for x in tools.due_reminders(due + 1):
        tools.fire_reminder(x, lambda u, t: fired.append(t))
    for x in tools.due_reminders(due + 1):  # duplicate tick never fires twice
        tools.fire_reminder(x, lambda u, t: fired.append(t))
    assert fired == ["⏰ Reminder: BP tablet"]
    assert store().get("contacts", f"rem:{r['rid']}")["due"] == due + 86400


def test_clarifying_question_means_no_action(monkeypatch):
    monkeypatch.setattr("app.chinku.planner.plan", lambda *a, **k: Plan(understood="", clarifying_question="Whose email?", confidence=0.4))
    executor.handle(ME, "email it", [], send, card)
    assert OUT == ["Whose email?"] and SENT == []


def test_compute_is_code_not_model():
    ctx = {"task": None, "card": card}
    assert tools.compute(ME, {"expression": "714 × 50"}, ctx)["value"] == 35700
    with pytest.raises(tools.ToolError):
        tools.compute(ME, {"expression": "__import__('os')"}, ctx)
