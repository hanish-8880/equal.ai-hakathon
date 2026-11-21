import pytest

from app.brain.schemas import CallBrief, ExtractedField, Extraction, MustCollectItem, RouteResult
from app.engine import runner, steps
from app.engine.specs_loader import arith, load_specs, render
from app.engine.validate import indian_mobile, mask_phone
from app.store.dynamo import MemoryStore, set_store, store


@pytest.fixture(autouse=True)
def mem_store():
    set_store(MemoryStore(path=None))
    yield


# ---------- spec loading ----------
def test_specs_load_and_valid():
    specs = load_specs()
    assert {"dealer_quotes", "banner_rate"} <= set(specs)
    for s in specs.values():
        for st in s["steps"]:
            assert st["type"] in {"call", "wait_for", "email", "schedule", "reply"}


def test_render_keeps_types_and_fills_strings():
    ctx = {"inputs": {"q": 50, "item": "cement"}, "item": {"phone": "+919876543210"}}
    assert render("{inputs.q}", ctx) == 50
    assert render("{inputs.item} dealer", ctx) == "cement dealer"
    assert render("{item.phone}", ctx) == "+919876543210"


# ---------- validation ----------
@pytest.mark.parametrize("raw,ok", [
    ("98765 43210", "+919876543210"), ("+91-9876543210", "+919876543210"), ("09876543210", "+919876543210"),
    ("919876543210", "+919876543210"), ("5876543210", None), ("98765", None), (None, None), ("abc", None),
])
def test_indian_mobile(raw, ok):
    assert indian_mobile(raw) == ok


def test_mask():
    assert mask_phone("+919876543210") == "******3210"


# ---------- comparison arithmetic (near-identical pair from CLAUDE.md §12) ----------
def _res(name, price, transport=None, status="confirmed"):
    f = {"price_per_unit": {"value": price, "status": status}}
    if transport is not None:
        f["transport_charge"] = {"value": transport, "status": "confirmed"}
    return {"callee": {"name": name}, "fields": f}


def test_transport_extra_changes_winner():
    schema = load_specs()["dealer_quotes"]["result_schema"]
    expr = "price_per_unit * quantity + transport_charge"
    a = [_res("A", 385), _res("B", 390)]
    assert steps.rank(a, expr, {"quantity": 50}, schema, "total")[0]["callee"] == "A"
    b = [_res("A", 385, 2000), _res("B", 390)]
    r = steps.rank(b, expr, {"quantity": 50}, schema, "total")
    assert r[0]["callee"] == "B" and r[0]["total"] == 19500 and r[1]["total"] == 21250


def test_missing_price_ranks_last():
    schema = load_specs()["dealer_quotes"]["result_schema"]
    rows = [{"callee": {"name": "X"}, "fields": {}}, _res("Y", 400)]
    r = steps.rank(rows, "price_per_unit * quantity + transport_charge", {"quantity": 10}, schema, "t")
    assert [x["callee"] for x in r] == ["Y", "X"] and r[1]["t"] is None


def test_arith_safe():
    assert arith("a*b+c", {"a": 2, "b": 3, "c": 1}) == 7
    assert arith("a*b", {"a": 2}) is None
    assert arith("__import__('os').system('x')", {}) is None  # never executed


# ---------- idempotency ----------
def test_claim_once():
    assert store().claim("wa:SM1") is True
    assert store().claim("wa:SM1") is False


# ---------- full flow with mocked brain + Omni ----------
@pytest.fixture
def fake(monkeypatch):
    dispatched = []

    def fake_route(msg, image, catalog, contacts, task_id=None):
        return RouteResult(spec="dealer_quotes", inputs={
            "item": "cement", "quantity": 50,
            "dealers": [{"name": "Ravi", "phone": "9876543210"}, {"name": "Sai", "phone": "9123456789"}]})

    def fake_brief(step, inputs, callee, prior, task_id=None):
        return CallBrief(on_behalf_of="Hanish", callee_role="dealer", language="te-en", max_minutes=3,
                         opening_line="AI assistant for Hanish", goal="price",
                         must_collect=[MustCollectItem(field=f, question_to_ask="?", acceptable_answer_example="x")
                                       for f in step["must_collect"]],
                         closing_line="thanks", call_script="script")

    def fake_dispatch(to, ctx):
        dispatched.append((to, ctx))
        return {"requestId": f"omni-{len(dispatched)}"}

    def fake_extract(transcript, must, schema, task_id=None):
        price = 385 if "385" in transcript else 390
        return Extraction(call_outcome="completed", follow_up_needed=False, fields=[
            ExtractedField(name="price_per_unit", value=price, status="confirmed", evidence_quote=str(price))])

    monkeypatch.setattr(runner.router, "route", fake_route)
    monkeypatch.setattr(steps.brief_writer, "write_brief", fake_brief)
    monkeypatch.setattr(steps, "DISPATCH", fake_dispatch)
    monkeypatch.setattr(steps.extractor, "extract", fake_extract)
    monkeypatch.setattr(steps.decider, "compose_reply", lambda hint, i, res, rank, lang, tid=None: f"best={rank[0]['callee']}")
    return dispatched


def test_flow_confirm_parallel_calls_dedupe_and_report(fake):
    sent = []
    runner.handle_message("whatsapp:+91me", "50 bags cement, call these", None, sent.append)
    t = store().scan("tasks")[0]
    assert t["status"] == "awaiting_confirm" and "Call cheyyana" in sent[-1]
    assert t["inputs"]["dealers"][0]["phone"] == "+919876543210"  # validated to E.164
    assert fake == []  # confirmation gate: no call before yes

    runner.handle_message("whatsapp:+91me", "yes", None, sent.append)
    assert len(fake) == 2  # parallel dispatch
    assert all(ctx["task_id"] == t["task_id"] for _, ctx in fake)
    calls = store().scan("calls")
    assert all(c["status"] == "dispatched" for c in calls)

    user_msgs = []
    to_user = lambda u, m: user_msgs.append(m)
    c1, c2 = sorted(calls, key=lambda c: c["created_at"])
    runner.on_call_result({"call_id": c1["omni_call_id"], "transcript": "rate 390 per bag"}, to_user)
    assert store().get("tasks", t["task_id"])["status"] == "running"  # waits for second call
    # duplicate webhook for c1 must not re-process
    runner.on_call_result({"call_id": c1["omni_call_id"], "transcript": "rate 390 per bag"}, to_user)
    runner.on_call_result({"call_id": c2["omni_call_id"], "transcript": "385 per bag"}, to_user)

    t = store().get("tasks", t["task_id"])
    assert t["status"] == "done"
    assert user_msgs == [f"best={c2['callee']['name']}"]  # exactly one report; c2 quoted 385
    assert len(fake) == 2  # duplicate never caused another call


def test_invalid_phone_asks_instead_of_calling(fake, monkeypatch):
    monkeypatch.setattr(runner.router, "route", lambda *a, **k: RouteResult(
        spec="banner_rate", inputs={"phone": "98765", "project_name": "Green Villas"}))
    sent = []
    runner.handle_message("whatsapp:+91me", "call this builder", {"image_type": "banner"}, sent.append)
    t = store().scan("tasks")[0]
    assert t["status"] == "planned" and "invalid:phone" in t["problems"]
    assert "number" in sent[-1].lower() and fake == []


def test_no_cancels(fake):
    sent = []
    runner.handle_message("u", "cement", None, sent.append)
    runner.handle_message("u", "no", None, sent.append)
    assert store().scan("tasks")[0]["status"] == "cancelled" and fake == []


def test_no_answer_retry_then_report(fake, monkeypatch):
    timers = []
    monkeypatch.setattr(steps.threading, "Timer", lambda d, fn, args: type("T", (), {"start": lambda s: timers.append((fn, args))})())
    sent, out = [], []
    runner.handle_message("u", "cement", None, sent.append)
    runner.handle_message("u", "yes", None, sent.append)
    c1, c2 = sorted(store().scan("calls"), key=lambda c: c["created_at"])
    runner.on_call_result({"call_id": c1["omni_call_id"], "transcript": ""}, lambda u, m: out.append(m))
    assert len(timers) == 1  # retry scheduled
    fn, args = timers[0]
    fn(*args)  # fire retry now
    assert len(fake) == 3
    retry = [c for c in store().scan("calls") if c["attempt"] == 1][0]
    runner.on_call_result({"call_id": retry["omni_call_id"], "transcript": ""}, lambda u, m: out.append(m))
    runner.on_call_result({"call_id": c2["omni_call_id"], "transcript": "385"}, lambda u, m: out.append(m))
    assert store().scan("tasks")[0]["status"] == "done" and out == [f"best={c2['callee']['name']}"]


def test_parse_omni_log_row_verified_shape():
    from app.voice.omni import parse_webhook
    row = {"call_request_id": {"id": 11127484, "type_of_request": "Call"}, "call_status": "completed",
           "call_conversation": "LLM: Namaste, I am Chinku.<br/>User: Rate 385.<br/>",
           "call_duration_in_seconds": 18.0,
           "custom_variables": {"task_id": "t1", "step_id": "s1", "chinku_call_id": "c_x"}}
    p = parse_webhook(row)
    assert p["call_id"] == "11127484" and p["chinku_call_id"] == "c_x" and p["task_id"] == "t1"
    assert p["transcript"] == "assistant: Namaste, I am Chinku.\nuser: Rate 385."


def test_email_gate_correct_confirm_send(monkeypatch):
    from app.brain.lookup import Found
    from app.brain.schemas import EmailDraft
    sent_mail = []
    monkeypatch.setattr(runner.router, "route", lambda *a, **k: RouteResult(spec="warranty_email", inputs={
        "brand": "Prestige", "product_type": "mixer grinder", "problem": "not working"}))
    monkeypatch.setattr(steps.lookup, "find_email", lambda q, tid=None: Found(email="care@brand.com", source_url="https://x"))
    monkeypatch.setattr(steps.decider, "draft_email", lambda *a, **k: EmailDraft(subject="Warranty", body="Hi"))
    monkeypatch.setattr(steps.email_action, "send", lambda to, s, b: sent_mail.append(to) or "me@x.com")
    monkeypatch.setattr(steps.decider, "compose_reply", lambda *a, **k: "mail sent")
    out = []
    runner.handle_message("u", "mixer broken, warranty mail pettu", None, out.append)
    t = store().scan("tasks")[0]
    assert t["status"] == "awaiting_confirm" and "care@brand.com" in out[-1] and sent_mail == []
    runner.handle_message("u", "use support@prestige.in instead", None, out.append)  # correction
    assert "support@prestige.in" in out[-1] and sent_mail == []
    runner.handle_message("u", "yes", None, out.append)
    assert sent_mail == ["support@prestige.in"]
    assert store().scan("tasks")[0]["status"] == "done" and out[-1] == "mail sent"


def test_photo_alone_remembered_for_next_message(monkeypatch):
    seen = []
    monkeypatch.setattr(runner.router, "route", lambda msg, image, *a, **k: seen.append(image) or RouteResult(spec=None, clarifying_question="Em cheyyali?"))
    runner.handle_message("u", "", {"image_type": "product", "fields": {"brand": "Bajaj"}}, lambda m: None)
    runner.handle_message("u", "warranty mail pettu", None, lambda m: None)
    assert seen[1] == {"image_type": "product", "fields": {"brand": "Bajaj"}}
    runner.handle_message("u", "another msg", None, lambda m: None)
    assert seen[2] is None  # used once only
