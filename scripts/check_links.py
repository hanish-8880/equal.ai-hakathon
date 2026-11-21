"""Is everything linked? Run ON THE SERVER: python -m scripts.check_links
Part 1 checks every external connection with the real config. Part 2 runs one complete plumber job end to end with the REAL brain
and REAL DynamoDB under a separate TEST identity, while phone calls, WhatsApp and email are SIMULATED (nobody is called or messaged)."""
import json
import time
import traceback
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

IST = timezone(timedelta(hours=5, minutes=30))
OK, BAD = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        try:
            ok, note = fn()
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"{e.__class__.__name__}: {str(e)[:160]}"
        results.append((name, ok, note))
        print(f"{OK if ok else BAD} {name}: {note}", flush=True)
        return fn
    return wrap


s = settings()


# ---------------- Part 1: connections ----------------
@check("Public HTTPS")
def _():
    r = httpx.get(f"{s.public_base_url}/health", timeout=15)
    return r.status_code == 200, f"{s.public_base_url} → {r.status_code}"


@check("DynamoDB read/write (instance role)")
def _():
    from app.store.dynamo import store
    st = store()
    k = f"linkcheck:{time.time()}"
    st.put("contacts", k, {"kind": "linkcheck"})
    return st.get("contacts", k) is not None, type(st).__name__


@check("Omni agents (webhook, voice, LLM, ASR) + number binding")
def _():
    H = {"Authorization": f"Bearer {s.omnidim_api_key}"}
    notes, ok = [], True
    for a in (s.omnidim_agent_id, s.omnidim_inbound_agent_id):
        d = httpx.get(f"{s.omnidim_base_url}/agents/{a}", headers=H, timeout=20).json()
        hooks = [p.get("webhook_url") for p in d.get("post_call_config_ids") or []]
        good = any(h == f"{s.public_base_url}/webhooks/omni" for h in hooks)
        ok &= good
        notes.append(f"{d['name']}: webhook {'OK' if good else hooks}, {d.get('llm_service')}, {d.get('asr_service')}, voice {d.get('voice_external_id', '')[:8]}")
    n = httpx.get(f"{s.omnidim_base_url}/phone_number/list?pageno=1&pagesize=5", headers=H, timeout=20).json()["phone_numbers"][0]
    bound = str(n["active_bot_id"]) == str(s.omnidim_inbound_agent_id)
    notes.append(f"{n['phone_number']} inbound → {'chinku-inbound' if bound else n['active_bot_id']}")
    return ok and bound, " | ".join(notes)


@check("Twilio account (WhatsApp)")
def _():
    a = (s.twilio_account_sid, s.twilio_auth_token)
    d = httpx.get(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}.json", auth=a, timeout=20).json()
    alerts = httpx.get("https://monitor.twilio.com/v1/Alerts?PageSize=1", auth=a, timeout=20).json().get("alerts", [])
    last = alerts[0] if alerts else None
    stale = last and "trycloudflare" in (last.get("request_url") or "")
    return d["type"] == "Full", f"type {d['type']}; last webhook alert: {(last or {}).get('date_created', 'none')}" + (" (stale tunnel URL seen — poller backstop covers it)" if stale else "")


@check("Gmail SMTP send + IMAP read")
def _():
    import imaplib
    import smtplib
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as m:
        m.login(s.smtp_user, s.smtp_app_password)
    i = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
    i.login(s.smtp_user, s.smtp_app_password)
    i.select("INBOX", readonly=True)
    i.logout()
    return True, f"{s.smtp_user} (demo override = {s.email_demo_override})"


@check("OpenAI models")
def _():
    from app.brain.client import oai
    ids = {m.id for m in oai().models.list()}
    need = [s.openai_model_brain, s.openai_model_fast, s.openai_model_transcribe, s.openai_model_tts]
    return all(m in ids for m in need), ", ".join(f"{m}{'' if m in ids else ' MISSING'}" for m in need)


@check("OpenStreetMap geocode")
def _():
    from app.actions import maps
    g = maps.geocode("Kondapur, Hyderabad")
    return bool(g), f"{g[0]:.4f},{g[1]:.4f}" if g else "no result"


@check("PDF + PPT libraries")
def _():
    import fpdf  # noqa: F401
    import pptx  # noqa: F401
    return True, "fpdf2 + python-pptx"


# ---------------- Part 2: one full plumber job (real brain + DynamoDB, simulated calls/messages) ----------------
@check("Full job: create → book (slot lock) → parts (compare + rule) → invoice ₹1,400 → idempotent send")
def _():
    from app.actions import email as email_action
    from app.engine import runner, steps
    from app.jobs import assistant, billing, ops, sourcing
    from app.jobs import store as jobs
    from app.store.dynamo import store
    U = f"whatsapp:+9190000{int(time.time()) % 100000:05d}"  # isolated TEST identity (never shown in the real app)
    dispatched, cards, sent = [], [], []
    steps.DISPATCH = lambda to, ctx: dispatched.append((to, ctx)) or {"requestId": f"sim-{len(dispatched)}-{time.time()}"}
    steps.NOTIFY = lambda to, text, audio=None: sent.append(("notify", to, text))
    ops.CARD = assistant.CARD = lambda to, text, card: cards.append((text, card))
    ops.NOTIFY = lambda to, text, audio=None: sent.append(("wa", to, text))
    email_action.send = lambda to, subj, body, att=None, filename=None: sent.append(("email", to, filename)) or to
    out = []
    store().put("contacts", f"{U}:srinivas", {"user": U, "alias": "srinivas", "phone": "+919000000001", "relation": "shop"})
    tomorrow = (datetime.now(IST) + timedelta(days=1)).strftime("%A")
    # 1) create the job (real understanding + extraction)
    assistant.handle(U, f"Chinku, new job. Customer 98480 12321, kitchen tap repair at Flat 304, Green Park, Kondapur. "
                        f"I'm free tomorrow 11 to 1 or after 4. Bill Sai Facility bills@sai.example under WO-117.", None, out.append)
    job = jobs.open_jobs(U)[-1]
    assert job.roles.site_contact.phone == "+919848012321" and len(job.appointment.plumber_available) >= 2, job.model_dump_json()[:400]
    # 2) booking: plumber asks → gate → yes → booking call (simulated answer: second window)
    assistant.handle(U, f"{job.job_id} customer ki call chesi visit book chey", None, out.append)
    if "go ahead" in (out[-1] if out else ""):
        assistant.handle(U, "yes", None, out.append)
    assert dispatched, f"no booking call dispatched; last reply: {out[-1] if out else cards[-1][0]}"
    w2 = job.appointment.plumber_available[1]
    call = [c for c in store().scan("calls", status="dispatched") if c["to_number"] == "+919848012321"][-1]
    runner.on_call_result({"call_id": call["omni_call_id"], "transcript":
        f"assistant: Which time suits you, 11 to 1 or 4 to 7 {tomorrow}?\nuser: 4 to 7 is fine. Flat 304, third floor, watchman will open."
        f"\nassistant: So {tomorrow} 4 to 7, Flat 304, watchman opens — right?\nuser: Yes."}, lambda u, m: out.append(m))
    job = jobs.get(job.job_id)
    assert job.appointment.state == "booked" and job.appointment.slot.start == w2.start, f"booking state {job.appointment.state} {job.next_action}"
    assert store().get("seen", ops.slot_key(U, w2.start)) is not None  # slot locked
    # 3) parts with the plumber's rule (real part extraction; simulated shop answer: different model, tomorrow)
    r = sourcing.start_sourcing(U, job, f"{job.job_id} ki rendu Jaquar angle valves ALD-0517 half inch kavali. Srinivas ki call chesi price kanukko. "
                                        "Srinivas daggara 500 lopu exact model unte customer ni 4 to 6 adugu.", None, out.append)
    shop_call = [c for c in store().scan("calls", status="dispatched") if c["to_number"] == "+919000000001"][-1]
    runner.on_call_result({"call_id": shop_call["omni_call_id"], "transcript":
        "assistant: Do you have Jaquar ALD-0517 half inch, 2 pieces, today?\nuser: Exact one comes tomorrow. Today I have ALD-0520.\n"
        "assistant: Price per piece?\nuser: 280 for the 0520. MRP 350.\nassistant: Pickup?\nuser: Kondapur main road shop, from 10 am."}, lambda u, m: out.append(m))
    job = jobs.get(job.job_id)
    offer_card = next((c for t, c in reversed(cards) if c and c.get("type") == "offers"), None)
    assert offer_card and job.dependencies and job.dependencies[-1].state == "not_met", f"deps {job.dependencies}"
    # 4) invoice from the closing note (real extraction) → readback → yes → send (simulated) → retry never duplicates
    billing.start_invoice(U, job, f"{job.job_id} finished. Fitted two angle valves at 350 each and one hose at 300. Labour 400. "
                                  "The third valve was unused and returned. Send the bill to the company.")
    job = jobs.get(job.job_id)
    assert job.invoice.total == 1400, f"total {job.invoice.total}"
    assistant.handle(U, "yes", None, out.append)
    n_sent = len([x for x in sent if x[0] == "email"])
    store().put("contacts", f"pendingbill:{U}", {"owner": U, "job_id": job.job_id, "kind": "invoice", "ts": time.time()})
    billing.send_pending(U, out.append)
    job = jobs.get(job.job_id)
    assert job.invoice.state == "sent" and n_sent == 1 and len([x for x in sent if x[0] == "email"]) == 1
    tl = [e.kind for e in job.timeline]
    return True, (f"{job.job_id}: booked {job.appointment.slot.label}, slot locked; Srinivas offer → rule NOT met "
                  f"({job.dependencies[-1].reason}); invoice {job.invoice.number} ₹{job.invoice.total:,.0f} sent once "
                  f"(email); {len(dispatched)} simulated calls; timeline {len(tl)} events")


print("\n" + "=" * 60)
for n, ok, note in results:
    print(f"{OK if ok else BAD} {n}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)} linked")
