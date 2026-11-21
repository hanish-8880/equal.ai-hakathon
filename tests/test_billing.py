import time

import pytest

from app.jobs import billing, ops
from app.jobs import store as jobs
from app.jobs.models import ApprovalRequest, Job, Person
from app.store.dynamo import MemoryStore, set_store, store

ME = "whatsapp:+919989683607"
CARDS: list = []


@pytest.fixture(autouse=True)
def mem(monkeypatch):
    set_store(MemoryStore(path=None))
    CARDS.clear()
    monkeypatch.setattr(ops, "CARD", lambda to, text, card: CARDS.append((text, card)))
    yield


def _job() -> Job:
    j = Job(job_id="J24", plumber_id=ME, created_at=time.time())
    j.roles.billing_to = Person(name="Sai Facility", email="bills@sai.example")
    j.roles.site_contact = Person(name="Ramesh", phone="+919848012321")
    j.roles.work_order = "WO-117"
    return jobs.save(j)


DRAFT = billing.InvoiceDraftOut(lines=[
    billing.LineDraft(item="angle valve", qty=2, unit_rate=350, evidence_quote="two angle valves at 350 each"),
    billing.LineDraft(item="hose", qty=1, unit_rate=300, evidence_quote="one hose at 300"),
    billing.LineDraft(item="angle valve", qty=1, returned=True, evidence_quote="third valve was unused and returned")],
    labour_amount=400, labour_evidence="Labour 400")


def test_the_1400_case():
    inv, issues, excluded = billing.compute_invoice(ME, _job(), DRAFT)
    assert inv.total == 1400 and [x.amount for x in inv.lines] == [700, 300] and inv.labour == 400
    assert excluded == ["1 × angle valve (returned / unused)"] and issues == []
    rb = billing.readback(_job(), inv, issues, excluded)
    assert "Total ₹1,400" in rb and "Sai Facility against WO-117" in rb


def test_spoken_total_conflict_is_flagged():
    d = DRAFT.model_copy(update={"spoken_total": 1500})
    _, issues, _ = billing.compute_invoice(ME, _job(), d)
    assert any("1,500" in i and "1,400" in i for i in issues)


def test_unapproved_extra_work_is_flagged_and_price_list_used_when_rate_not_said():
    j = _job()
    j.extra_work.append(ApprovalRequest(id="A1", scope="hose replacement", amount=300, response="pending"))
    d = billing.InvoiceDraftOut(lines=[billing.LineDraft(item="connection hose", qty=1)], labour_hours=1)
    inv, issues, _ = billing.compute_invoice(ME, j, d)
    assert inv.lines[0].unit_rate == 300 and "price list (demo)" in inv.lines[0].source and inv.labour == 400
    assert any("not approved" in i for i in issues)


def test_send_is_idempotent(monkeypatch):
    sent = []
    from app.actions import email as email_action
    monkeypatch.setattr(email_action, "send", lambda to, s, b, att=None, filename=None: sent.append((to, filename)) or to)
    j = _job()
    inv, _, _ = billing.compute_invoice(ME, j, DRAFT)
    j.invoice = inv
    jobs.save(j)
    store().put("contacts", f"pendingbill:{ME}", {"owner": ME, "job_id": "J24", "kind": "invoice", "ts": time.time()})
    assert billing.send_pending(ME, lambda m: None)
    first = jobs.get("J24").invoice
    assert first.state == "sent" and len(sent) == 1 and sent[0][1].startswith("INV-") and sent[0][1].endswith(".pdf")
    store().put("contacts", f"pendingbill:{ME}", {"owner": ME, "job_id": "J24", "kind": "invoice", "ts": time.time()})
    billing.send_pending(ME, lambda m: None)  # retry / double tap
    assert len(sent) == 1 and jobs.get("J24").invoice.number == first.number  # never duplicated, same number


def test_unknown_customer_gets_receptionist_reply_and_plumber_alert(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m.settings(), "user_whatsapp", ME)
    sent, alerts = [], []
    monkeypatch.setattr(m.whatsapp, "send", lambda to, body, media=None: sent.append((to, body)))
    monkeypatch.setattr(m, "deliver", lambda to, text, media_url=None, card=None: alerts.append((to, text)))
    m.process_customer({"from": "whatsapp:+919000011111", "body": "pipe burst, water flooding the kitchen", "media": []}, [])
    assert "turn off the main valve" in sent[0][1] and alerts[0][0] == ME and "EMERGENCY" in alerts[0][1]


def test_owner_number_counts_as_whatsapp_joined(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(), "user_whatsapp", "whatsapp:+919989683607")
    assert ops.wa_joined("+919989683607") and not ops.wa_joined("+919000000000")
    store().put("contacts", "wajoined:+919000000000", {"phone": "+919000000000"})
    assert ops.wa_joined("+919000000000")


def test_invoice_pdf_download_needs_key_and_returns_pdf(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    monkeypatch.setattr(m.settings(), "user_whatsapp", ME)
    monkeypatch.setattr(m.settings(), "chat_key", "k123")
    j = _job()
    j.invoice, _, _ = billing.compute_invoice(ME, j, DRAFT)
    j.invoice.state = "sent"; j.invoice.number = "INV-TEST-1"
    jobs.save(j)
    c = TestClient(m.app)
    assert c.get("/api/jobs/J24/pdf?kind=invoice&key=wrong").status_code == 403
    r = c.get("/api/jobs/J24/pdf?kind=invoice&key=k123")
    assert r.status_code == 200 and r.content[:4] == b"%PDF" and "INV-TEST-1.pdf" in r.headers["content-disposition"]
    assert c.get("/api/jobs/J24/pdf?kind=estimate&key=k123").status_code == 404  # no estimate on this job


def test_nearby_shops_by_gps_area_or_job(monkeypatch):
    import threading
    from app.jobs import assistant, sourcing
    calls = []
    monkeypatch.setattr(sourcing, "nearby_at", lambda *a: calls.append(("at",) + a))
    monkeypatch.setattr(sourcing, "_nearby_card", lambda user, job: calls.append(("job", job.job_id)))
    monkeypatch.setattr(threading, "Thread", lambda target, args, daemon=True: type("T", (), {"start": lambda self: target(*args)})())
    out = []
    assistant.handle(ME, "nearby shops @17.45880,78.37310", None, out.append)
    assistant.handle(ME, "shops near Madhapur", None, out.append)
    _job()
    assistant.handle(ME, "nearby shops", None, out.append)
    assert calls[0] == ("at", ME, "your location", 17.4588, 78.3731)
    assert calls[1] == ("at", ME, "Madhapur") and calls[2] == ("job", "J24")
