"""A CUSTOMER called Chinku's number and booked a visit on the call (CLAUDE.md §6.2, inbound side).
LLM: read the transcript into fields with evidence quotes. CODE: map the chosen slot onto the plumber's real
working windows, lock it with a conditional write (two callers can never take one slot), create the job,
confirm to the customer (WhatsApp if joined) and show the plumber a card. Nothing is booked on a guess."""
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.brain.client import json_call, prompt
from app.brain.schemas import FactOut
from app.config import settings
from app.engine.validate import indian_mobile, mask_phone
from app.jobs import ops, slots
from app.jobs import store as jobs
from app.jobs.models import Fact, Job, NextAction, Person, Window
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))


class CustomerCallOut(BaseModel):
    caller_kind: Literal["customer", "plumber", "unclear"] = "unclear"
    caller_name: FactOut = Field(default_factory=FactOut)
    issue: FactOut = Field(default_factory=FactOut)
    site_address: FactOut = Field(default_factory=FactOut)
    flat: FactOut = Field(default_factory=FactOut)
    access_notes: FactOut = Field(default_factory=FactOut)
    chosen_slot_id: FactOut = Field(default_factory=FactOut)
    preferred_time_text: str | None = None
    emergency: bool = False
    readback_confirmed: bool = False


def read_call(transcript: str, windows: list[dict[str, Any]]) -> CustomerCallOut:
    payload = json.dumps({"now_ist": datetime.now(IST).isoformat(), "plumber": settings().plumber_name,
                          "open_slots": [{"id": w["id"], "label": w["label"]} for w in windows], "transcript": transcript[:6000]},
                         ensure_ascii=False)
    return json_call(settings().openai_model_brain, prompt("customer_call_extract"), payload, CustomerCallOut, None, "customer_call")


def _fact(f: FactOut, ref: str) -> Fact:
    return Fact(value=f.value, status=f.status, evidence_quote=f.evidence_quote, source=ref) if f.status in ("confirmed", "tentative") \
        else Fact(status=f.status, source=ref)


def handle(parsed: dict[str, Any], read: CustomerCallOut | None = None) -> Job | None:
    """Create the job from the customer's call; book the slot only if code can lock it."""
    s = settings()
    owner = s.user_whatsapp
    phone = indian_mobile(parsed.get("from_number") or "")
    ref = f"inbound:{parsed.get('call_id') or parsed.get('omni_call_id') or int(time.time())}"
    windows = slots.work_windows()
    for i, w in enumerate(windows, start=1):
        w["id"] = f"W{i}"
    r = read or read_call(parsed.get("transcript", ""), windows)
    if r.caller_kind == "plumber":
        return None
    if not (r.issue.value or r.site_address.value or r.chosen_slot_id.value):
        ops.CARD(owner, f"📞 Missed enquiry: {mask_phone(phone)} called but I couldn't get the details "
                        f"({parsed.get('summary') or 'no summary'}). Call them back?", {"type": "note"})
        return None
    job = Job(job_id=jobs.new_job_id(), plumber_id=owner, created_at=time.time(),
              issue=_fact(r.issue, ref), site_address=_fact(r.site_address, ref), flat=_fact(r.flat, ref), access_notes=_fact(r.access_notes, ref))
    job.roles.site_contact = Person(name=r.caller_name.value if r.caller_name.status in ("confirmed", "tentative") else None,
                                    phone=phone, whatsapp_joined=ops.wa_joined(phone))
    job.appointment.plumber_available = [Window(**{k: w[k] for k in ("start", "end", "label")}) for w in slots.open_slots(owner)[:8]]
    jobs.log(job, "job_created", f"Customer called Chinku ({mask_phone(phone)}): {r.issue.value or 'issue not clear'}",
             actor="client", channel="call", ref=ref)
    sid = r.chosen_slot_id.value if r.chosen_slot_id.status in ("confirmed", "tentative") else None
    w = next((x for x in windows if x["id"] == sid), None)
    job.appointment.requested_by_client = Fact(value=w["label"] if w else r.preferred_time_text, status=r.chosen_slot_id.status if w else
                                               ("tentative" if r.preferred_time_text else "not_mentioned"),
                                               evidence_quote=r.chosen_slot_id.evidence_quote, source=ref)
    booked, why = False, ""
    if not w:
        why = f"asked for {r.preferred_time_text}" if r.preferred_time_text else "no time agreed on the call"
    elif datetime.fromisoformat(w["end"]) <= datetime.now(IST):
        why = f"{w['label']} is already over"
    elif not (r.readback_confirmed or r.chosen_slot_id.status == "confirmed"):
        why = f"{w['label']} was not clearly confirmed"
    elif not store().claim(ops.slot_key(owner, w["start"])):
        nxt = slots.open_slots(owner)
        why = f"{w['label']} was just taken by another booking" + (f" — next free: {nxt[0]['label']}" if nxt else "")
        jobs.log(job, "slot_conflict", why, channel="code", ref=ref)
    else:
        booked = True
    if booked:
        job.appointment.slot = Window(start=w["start"], end=w["end"], label=w["label"])
        job.appointment.state = "booked"
        job.appointment.confirmed_both = True  # caller accepted + it is inside the plumber's working hours + lock won
        job.appointment.history.append({"ts": time.time(), "event": "booked_by_customer_call", "slot": w, "ref": ref})
        job.status = "booked"
        job.next_action = NextAction(what=f"Visit {w['label']}", owner="plumber")
        jobs.log(job, "visit_booked", f"Visit booked {w['label']} by the customer on the call (“{r.chosen_slot_id.evidence_quote or w['label']}”)",
                 actor="client", channel="call", ref=ref)
        slots.sync_soon()  # the phone agent must stop offering this slot
    else:
        job.appointment.state = "pending"
        job.next_action = NextAction(what="Confirm a visit time with the customer", owner="plumber", blocked_by=why)
        jobs.log(job, "booking_pending", f"Not booked: {why}", actor="client", channel="call", ref=ref)
    jobs.save(job)
    # customer confirmation (WhatsApp only if their number joined the sandbox — never pretend otherwise)
    name = job.roles.site_contact.name
    where = " ".join(str(x) for x in (job.flat.value, job.site_address.value) if x)
    if booked:
        msg = (f"Hello{(' ' + name) if name else ''}, this is Chinku, assistant of plumber {s.plumber_name}. Your visit is booked: "
               f"{w['label']}{(' at ' + where) if where else ''} for {job.issue.value or 'the plumbing work'}. Job {job.job_id}. Reply here if anything changes.")
    else:
        msg = (f"Hello{(' ' + name) if name else ''}, this is Chinku, assistant of plumber {s.plumber_name}. We got your request"
               f"{(' for ' + str(job.issue.value)) if job.issue.value else ''}. {s.plumber_name} will confirm the visit time with you shortly. Job {job.job_id}.")
    if phone and ops.wa_joined(phone):
        ops.NOTIFY(f"whatsapp:{phone}", msg, None)
        jobs.log(job, "client_notified", f"WhatsApp {'confirmation' if booked else 'acknowledgement'} sent to {mask_phone(phone)}", channel="whatsapp")
    else:
        jobs.log(job, "client_notified", f"{mask_phone(phone)} has not joined WhatsApp — told on the call only", channel="call")
    jobs.save(job)
    head = (f"📞 New booking from a customer call ✅ {job.job_id} · {w['label']}" if booked
            else f"📞 New customer call — {job.job_id} NOT booked yet ({why}). Which time should I confirm?")
    if r.emergency:
        head = "🚨 EMERGENCY — " + head
    ops.CARD(owner, f"{head}\nCaller {mask_phone(phone)}{(' · ' + name) if name else ''} · {job.issue.value or '?'} · {where or 'address not clear'}",
             jobs.card(job))
    return job
