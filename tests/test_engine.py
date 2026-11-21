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
            assert st["type"] in {"call", "wait_for", "email", "schedule", "reply", "compute", "fetch_email", "make_doc"}


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
    assert t["status"] == "awaiting_confirm" and "Shall I call" in sent[-1]
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


def test_passbook_flow_wait_for_photo_then_explain_and_voice(fake, monkeypatch):
    notes = []
    monkeypatch.setattr(steps, "NOTIFY", lambda to, text, audio=None: notes.append((to, text, audio)))
    monkeypatch.setattr(steps.perceive, "tts", lambda text: b"mp3")
    monkeypatch.setattr(steps.threading, "Timer", lambda d, fn, args: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(steps.decider, "compose_reply", lambda hint, i, res, rank, lang, tid=None: f"[{lang}] {hint[:20]}")
    seen_brief_prior = []
    orig_brief = steps.brief_writer.write_brief
    monkeypatch.setattr(steps.brief_writer, "write_brief", lambda step, inputs, callee, prior, tid=None: seen_brief_prior.append(prior) or orig_brief(step, inputs, callee, prior, tid))
    monkeypatch.setattr(runner.router, "route", lambda msg, image, catalog, contacts, tid=None: RouteResult(
        spec="passbook_explain", inputs={"person_name": "Amma", "person_phone": contacts[0]["phone"]}))
    monkeypatch.setattr(steps.extractor, "extract", lambda tr, must, schema, tid=None: Extraction(
        call_outcome="completed", follow_up_needed=False,
        fields=[ExtractedField(name=m, value=True, status="confirmed", evidence_quote="sare") for m in must]))
    me, amma = "whatsapp:+919000000001", "whatsapp:+919876543210"
    out = []
    runner.handle_message(me, "contact mummy 98765 43210", None, out.append)
    assert "Saved" in out[-1] and store().get("contacts", f"{me}:mummy")["phone"] == "+919876543210"
    runner.handle_message(me, "Mummy ki passbook ardham kaavatledu, explain chey", None, out.append)
    runner.handle_message(me, "yes", None, out.append)
    assert len(fake) == 1 and fake[0][0] == "+919876543210"
    c1 = store().scan("calls")[0]
    runner.on_call_result({"call_id": c1["omni_call_id"], "transcript": "user: sare pampista"}, lambda u, m: None)
    t = store().scan("tasks")[0]
    assert t["status"] == "waiting_input" and t["steps"]["get_photo"]["status"] == "waiting"
    # a text from mummy (not an image) does not satisfy the wait; owner's image goes elsewhere
    assert steps.find_waiting(amma, False) is None
    runner.handle_message(amma, "", {"image_type": "passbook", "fields": {"entries": [{"date": "01-11", "credit": 12000}]}}, out.append)
    assert "got the photo" in out[-1] and len(fake) == 2  # explain call dispatched
    assert seen_brief_prior[-1]["get_photo"]["image"]["fields"]["entries"][0]["credit"] == 12000
    c2 = [c for c in store().scan("calls") if c["step_id"] == "explain"][0]
    runner.on_call_result({"call_id": c2["omni_call_id"], "transcript": "user: ardham ayyindi"}, lambda u, m: notes.append((u, m, None)))
    t = store().scan("tasks")[0]
    assert t["status"] == "done"
    voice = [n for n in notes if n[2] == b"mp3"]
    assert len(voice) == 1 and voice[0][0] == amma and "English" in voice[0][1]
    assert notes[-1][0] == me  # final report to owner


def test_concurrency_limit_queues_calls(fake, monkeypatch):
    """Omni plan = 1 concurrent call: second dealer call waits in queue instead of failing."""
    monkeypatch.setattr(steps, "QUEUE_POLL_S", 0.01)
    busy = {"n": 0}
    real = steps.DISPATCH
    def limited(to, ctx):
        if busy["n"] < 3:  # first 3 attempts hit the limit
            busy["n"] += 1
            raise RuntimeError('429: {"error": "concurrency_limit_exceeded"}')
        return real(to, ctx)
    monkeypatch.setattr(steps, "DISPATCH", limited)
    runner.handle_message("u", "cement", None, lambda m: None)
    runner.handle_message("u", "yes", None, lambda m: None)
    calls = store().scan("calls")
    assert len(fake) == 2 and all(c["status"] == "dispatched" for c in calls)
    assert any(c.get("waited_in_queue") for c in calls)


def test_appointment_flow_schedule_fires_reminder(fake, monkeypatch):
    import time as _t
    from app.config import settings as _settings
    notes = []
    monkeypatch.setattr(steps, "NOTIFY", lambda to, text, audio=None: notes.append((to, text)))
    monkeypatch.setattr(steps.decider, "compose_reply", lambda hint, i, res, rank, lang, tid=None: hint[:80])
    monkeypatch.setattr(runner.router, "route", lambda msg, image, catalog, contacts, tid=None: RouteResult(
        spec="appointment", inputs={"person_phone": "9876543210", "clinic_phone": "9123456789", "clinic_name": "Care Plus"}))
    slot = "2030-01-01T11:00:00+05:30"
    def ex(tr, must, schema, tid=None):
        vals = {"slot_datetime": slot, "doctor_name": "Dr Rao", "consultation_fee": 500, "time_ok": True, "acknowledged": True}
        return Extraction(call_outcome="completed", follow_up_needed=False,
                          fields=[ExtractedField(name=m, value=vals[m], status="confirmed", evidence_quote="q") for m in must])
    monkeypatch.setattr(steps.extractor, "extract", ex)
    out = []
    runner.handle_message("me", "repu mummy ki doctor appointment book chey", None, out.append)
    runner.handle_message("me", "yes", None, out.append)
    def finish_last_call():
        [c] = [c for c in store().scan("calls") if c["status"] == "dispatched"]
        runner.on_call_result({"call_id": c["omni_call_id"], "transcript": "user: ok"}, lambda u, m: notes.append((u, m)))
        return c
    assert finish_last_call()["to_number"] == "+919123456789"  # clinic first
    assert finish_last_call()["to_number"] == "+919876543210"  # then mummy
    t = store().scan("tasks")[0]
    st = t["steps"]["wait_until_reminder"]
    assert st["status"] == "scheduled" and t["status"] == "running"
    from datetime import datetime
    assert st["real_fire_at"] == datetime.fromisoformat(slot).timestamp() - 3600
    assert any("appointment booked" in n[1] for n in notes)  # report sent BEFORE the reminder
    assert runner.tick_schedules() == 0 and len(fake) == 2  # not due yet
    assert steps.due_schedules(st["fire_at"] + 1) == [(t["task_id"], "wait_until_reminder")]
    runner.fire_schedule(t["task_id"], "wait_until_reminder")
    assert runner.fire_schedule(t["task_id"], "wait_until_reminder") is False  # idempotent
    assert len(fake) == 3 and fake[-1][0] == "+919876543210"  # reminder call
    finish_last_call()
    assert store().scan("tasks")[0]["status"] == "done"


def test_schedule_bad_datetime_fails_task():
    task = {"task_id": "t", "steps": {"a": {"output": [{"fields": {"slot_datetime": {"value": "sometime", "status": "tentative"}}}]}}}
    out = []
    r = steps.run_schedule({"id": "s", "at_from": {"step": "a", "field": "slot_datetime"}, "offset_min": -60}, {}, task, out.append)
    assert r["status"] == "failed" and "time" in out[0]


def test_inr_format():
    assert steps.inr(64965) == "64,965" and steps.inr(6500000) == "65,00,000" and steps.inr(-1000) == "-1,000"
    assert steps.inr(385.5) == "385.50" and steps.inr(12) == "12"


def test_doc_total_email_flow_compute_in_code(monkeypatch):
    from app.brain.schemas import EmailDraft
    doc = {"image_type": "document", "doc_type": "invoice", "title": "SSB", "stated_total": 64965,
           "line_items": [{"label": "cement", "amount": 19250}, {"label": "steel", "amount": 31500},
                          {"label": "sand", "amount": 9000}, {"label": "transport", "amount": 2000},
                          {"label": "loading", "amount": 750}, {"label": "gst", "amount": 3465}, {"label": "disc", "amount": -1000}]}
    goals, sent, replies = [], [], []
    monkeypatch.setattr(runner.router, "route", lambda msg, image, catalog, contacts, tid=None: RouteResult(
        spec="doc_total_email", inputs={"recipient_name": "Suresh", "recipient_email": contacts[0]["email"]})
        if msg.strip() else RouteResult(spec=None))
    monkeypatch.setattr(steps.decider, "draft_email", lambda goal, *a, **k: goals.append(goal) or EmailDraft(subject="Total", body=goal))
    monkeypatch.setattr(steps.email_action, "send", lambda to, s, b: sent.append(to) or to)
    monkeypatch.setattr(steps.decider, "compose_reply", lambda hint, i, res, rank, lang, tid=None: replies.append(res) or hint[:40])
    out = []
    runner.handle_message("me", "contact suresh suresh@example.com", None, out.append)
    assert "suresh@example.com" in out[-1]
    runner.handle_message("me", "", doc, out.append)                                   # PDF alone
    runner.handle_message("me", "total cheppu, suresh ki mail pettu", None, out.append)  # instruction later
    t = store().scan("tasks")[0]
    assert t["inputs"]["document"]["stated_total"] == 64965  # filled deterministically, not by the router
    tot = t["steps"]["total"]["output"]
    assert tot["total"] == 64965 and tot["matches_stated"] is True and tot["total_str"] == "64,965"
    assert "₹64,965" in goals[0] and t["status"] == "awaiting_confirm" and sent == []
    runner.handle_message("me", "yes", None, out.append)
    assert sent == ["suresh@example.com"] and store().scan("tasks")[0]["status"] == "done"


def test_compute_flags_mismatch_and_missing():
    task = {"task_id": "t", "inputs": {"d": {"line_items": [{"label": "a", "amount": "1,000"}, {"label": "b", "amount": None}],
                                             "stated_total": 1500}}, "steps": {}}
    r = steps.run_compute({"id": "c", "list": "inputs.d.line_items", "field": "amount", "check_against": "inputs.d.stated_total"}, {}, task, None)
    o = r["output"]
    assert o["total"] == 1000 and o["matches_stated"] is False and o["missing"] == ["b"]


def test_inbound_call_only_owner_becomes_task(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m.settings(), "user_whatsapp", "whatsapp:+919989683607")
    got, sent = [], []
    monkeypatch.setattr(m.runner, "handle_message", lambda user, text, image, send, **kw: got.append((user, text)))
    monkeypatch.setattr(m.whatsapp, "send", lambda to, body, media=None: sent.append(to))
    base = {"direction": "inbound", "transcript": "assistant: Hello\nuser: PDF total Suresh ki mail pettu\nuser: avunu", "summary": "s"}
    m.process_omni({**base, "from_number": "+918888888888"})  # stranger
    assert got == []
    m.process_omni({**base, "from_number": "+919989683607"})
    assert got and got[0][0] == "whatsapp:+919989683607" and "Suresh ki mail" in got[0][1] and "Hello" not in got[0][1]


def test_message_during_gate_does_not_replan(monkeypatch):
    from app.brain.lookup import Found
    from app.brain.schemas import EmailDraft
    calls = []
    monkeypatch.setattr(runner.router, "route", lambda *a, **k: calls.append(1) or RouteResult(spec="warranty_email", inputs={
        "brand": "Prestige", "product_type": "mixer", "problem": "broken"}))
    monkeypatch.setattr(steps.lookup, "find_email", lambda q, tid=None: Found(email="c@b.com"))
    monkeypatch.setattr(steps.decider, "draft_email", lambda *a, **k: EmailDraft(subject="S", body="B"))
    out = []
    runner.handle_message("u", "mixer broken mail pettu", None, out.append)
    runner.handle_message("u", "inka emaina?", None, out.append)
    assert len(calls) == 1 and "Draft mail" in out[-1] and store().scan("tasks")[0]["status"] == "awaiting_confirm"


def test_call_from_document_dynamic_questions(fake, monkeypatch):
    seen_extract, seen_brief = [], []
    qs = [{"field": "policy_status", "question": "Is policy active?"}, {"field": "premium_due", "question": "When is premium due?"}]
    monkeypatch.setattr(runner.router, "route", lambda msg, image, catalog, contacts, tid=None: RouteResult(
        spec="call_from_document", inputs={"purpose": "Get policy details", "questions": qs}) if msg.strip() else RouteResult(spec=None))
    orig_brief = steps.brief_writer.write_brief
    monkeypatch.setattr(steps.brief_writer, "write_brief", lambda step, *a, **k: seen_brief.append(step) or orig_brief(step, *a, **k))
    monkeypatch.setattr(steps.extractor, "extract", lambda tr, must, schema, tid=None: seen_extract.append((must, schema)) or Extraction(
        call_outcome="completed", follow_up_needed=False,
        fields=[ExtractedField(name=m, value="x", status="confirmed", evidence_quote="x") for m in must]))
    monkeypatch.setattr(steps.decider, "compose_reply", lambda *a, **k: "report")
    paper = {"image_type": "other", "fields": {"contacts": [
        {"label": "LIC customer care", "phone": "98480 12345", "role": "insurance helpline"},
        {"label": "Agent Ravi", "phone": "9000012345", "role": "insurance agent"},
        {"label": "Landline", "phone": "040-2345", "role": "office"}]}}
    out = []
    runner.handle_message("u", "", paper, out.append)
    runner.handle_message("u", "ee paper lo numbers ki call chesi policy details adugu", None, out.append)
    t = store().scan("tasks")[0]
    assert [x["label"] for x in t["inputs"]["targets"]][:2] == ["LIC customer care", "Agent Ravi"]
    assert len(t["inputs"]["targets"]) == 2 and t["status"] == "awaiting_confirm"  # landline skipped, not blocking
    assert "Skipping" in out[-1] and "040-2345" in out[-1]
    runner.handle_message("u", "yes", None, out.append)
    assert len(fake) == 2 and seen_brief[0]["must_collect"] == ["policy_status", "premium_due"]
    assert seen_brief[0]["field_questions"]["premium_due"] == "When is premium due?"
    for _ in range(2):
        [c] = [c for c in store().scan("calls") if c["status"] == "dispatched"][:1]
        runner.on_call_result({"call_id": c["omni_call_id"], "transcript": "user: active, due 5 Dec"}, lambda u, m: out.append(m))
    assert seen_extract[0][0] == ["policy_status", "premium_due"] and seen_extract[0][1]["premium_due"]["question"]
    t = [x for x in store().scan("tasks") if x["status"] != "cancelled"][0]
    assert t["status"] == "done" and out[-1] == "report"


def test_inbound_task_always_needs_whatsapp_yes(monkeypatch):
    from app.brain.schemas import EmailDraft
    ran = []
    monkeypatch.setattr(runner.router, "route", lambda *a, **k: RouteResult(spec="email_pdf_report", inputs={"request": "total", "from_hint": "boss"}))
    monkeypatch.setattr(steps, "run_fetch_email", lambda *a, **k: ran.append(1) or {"status": "failed"})
    out = []
    runner.handle_message("u", "[call] boss pdf total", None, out.append, force_confirm=True)
    t = store().scan("tasks")[0]
    assert t["status"] == "awaiting_confirm" and ran == [] and "From your call" in out[-1] and "boss" in out[-1]
    runner.handle_message("u", "yes", None, out.append)
    assert ran == [1]


def test_web_chat_key_and_delivery(monkeypatch):
    import app.main as m
    from fastapi.testclient import TestClient
    monkeypatch.setattr(m.settings(), "chat_key", "k123")
    monkeypatch.setattr(m.settings(), "user_whatsapp", "whatsapp:+919989683607")
    wa = []
    monkeypatch.setattr(m.whatsapp, "send", lambda to, body, media=None: wa.append(body))
    monkeypatch.setattr(m, "process_message", lambda user, text, media: m.deliver(user, f"echo {text}"))
    c = TestClient(m.app)
    assert c.get("/chat?key=wrong").status_code == 403
    assert c.post("/api/chat", data={"key": "bad", "text": "x"}).status_code == 403
    assert c.post("/api/chat", data={"key": "k123", "text": "hi"}).status_code == 200
    rows = c.get("/api/chat/messages?key=k123").json()
    assert [r["dir"] for r in rows] == ["in", "out"] and rows[1]["text"] == "echo hi"
    assert wa == []  # last message came from web → no WhatsApp send
    m.set_channel("whatsapp:+919989683607", "whatsapp")
    m.deliver("whatsapp:+919989683607", "x")
    assert wa == ["x"]  # WhatsApp user → mirrored to web AND sent on WhatsApp


def test_stale_pending_task_expires_and_does_not_capture(monkeypatch):
    routed = []
    monkeypatch.setattr(runner.router, "route", lambda msg, *a, **k: routed.append(msg) or RouteResult(spec=None, clarifying_question="?"))
    store().put("tasks", "old", {"task_id": "old", "user": "u", "spec": "dealer_quotes", "status": "planned", "inputs": {},
                                 "steps": {}, "created_at": 0})
    store().data["tasks"]["old"]["updated_at"] = 0  # 1970 → stale
    runner.handle_message("u", "new request", None, lambda m: None)
    assert "[PENDING TASK" not in routed[0] and store().get("tasks", "old")["status"] == "cancelled"


def test_photo_numbers_fill_dealers(fake, monkeypatch):
    monkeypatch.setattr(runner.router, "route", lambda *a, **k: RouteResult(spec="dealer_quotes", inputs={"item": "chairs", "quantity": 3}))
    out = []
    img = {"image_type": "other", "fields": {"contacts": [{"label": "Shop", "phone": "9876543210"}]}}
    runner.handle_message("u", "ask chair price from this shop", img, out.append)
    t = store().scan("tasks")[0]
    assert t["inputs"]["dealers"][0]["phone"] == "+919876543210" and "Shall I call" in out[-1]


def test_stuck_call_times_out_and_task_finishes(fake, monkeypatch):
    monkeypatch.setattr(steps.decider, "compose_reply", lambda *a, **k: "report")
    import app.voice.omni as om
    monkeypatch.setattr(om, "fetch_log", lambda rid: None)
    out = []
    runner.handle_message("u", "cement", None, out.append)
    runner.handle_message("u", "yes", None, out.append)
    notes = []
    monkeypatch.setattr(steps, "NOTIFY", lambda to, text, audio=None: notes.append(text))
    later = max(c["created_at"] for c in store().scan("calls")) + 700
    assert runner.sweep_stuck_calls(later) == 2
    assert all(c["status"] == "failed" for c in store().scan("calls"))
    assert store().scan("tasks")[0]["status"] == "done" and notes == ["report"]
