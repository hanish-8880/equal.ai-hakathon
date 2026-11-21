"""Chinku for Plumbers — one router, one job store, many channels (CLAUDE.md v2 §3, §6).
Plumber-office intents run against a persistent Job. Anything else falls through to the v1 spec engine."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.brain.client import json_call, prompt
from app.brain.schemas import JobDraft, PlumberRoute
from app.config import settings
from app.engine import runner
from app.engine.validate import indian_mobile, mask_phone
from app.jobs import store as jobs
from app.jobs.models import Fact, Job, NextAction, Person, Window
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
Send = Callable[[str], None]
# CARD(to, text, card): deliver a message with a structured card (job card, offers, invoice). Set by main.py.
CARD: Callable[[str, str, dict[str, Any]], None] = lambda to, text, card: None
BUILT = {"create_job", "status_query", "small_talk", "correct_detail"}
NEXT_INTENTS = {"book_visit": "booking call with the customer", "source_part": "part sourcing calls to shops",
                "set_dependency": "part-before-visit rules", "request_approval": "extra-work approvals",
                "approval_response": "extra-work approvals", "close_job_invoice": "closing note → invoice",
                "report_delay": "delay calls", "cancel": "cancellation"}


def _now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30 (%A)")


def route(user: str, text: str, image: dict[str, Any] | None) -> PlumberRoute:
    s = settings()
    open_ = [{"job_id": j.job_id, "issue": j.issue.value, "site": j.flat.value or j.site_address.value, "status": j.status}
             for j in jobs.open_jobs(user)]
    contacts = [{k: c.get(k) for k in ("alias", "phone", "email")} for c in store().scan("contacts", user=user)]
    payload = json.dumps({"message": text, "attachment": image, "open_jobs": open_, "contacts": contacts}, ensure_ascii=False)
    return json_call(s.openai_model_brain, prompt("router_plumber").replace("{plumber_name}", s.plumber_name),
                     payload, PlumberRoute, None, "route_plumber")


def resolve_job(user: str, r: PlumberRoute) -> tuple[Job | None, str | None]:
    """Explicit job ref → that job. No ref → the only open job. Several open jobs → ask (never guess)."""
    if r.job_ref and re.fullmatch(r"J\d+", r.job_ref.upper()):
        j = jobs.get(r.job_ref.upper())
        if j and j.plumber_id == user:
            return j, None
        return None, f"I couldn't find job {r.job_ref.upper()}. Open jobs: " + (", ".join(x.job_id for x in jobs.open_jobs(user)) or "none")
    open_ = jobs.open_jobs(user)
    if len(open_) == 1:
        return open_[0], None
    if not open_:
        return None, "You don't have an open job yet. Tell me the customer, the problem and the address to create one."
    return None, "Which job? " + ", ".join(f"{j.job_id} ({j.issue.value or '?'}, {j.flat.value or j.site_address.value or '?'})" for j in open_)


def _fact(f: Any, source: str) -> Fact:
    return Fact(value=f.value, status=f.status, evidence_quote=f.evidence_quote, source=source)


def _person(p: Any) -> tuple[Person | None, list[str]]:
    if not p:
        return None, []
    phones = []
    for raw in p.phones:
        e = indian_mobile(raw)
        if e and e not in phones:
            phones.append(e)
    return Person(name=p.name, phone=phones[0] if len(phones) == 1 else None, email=p.email), phones


def create_job(user: str, text: str, msg_ref: str) -> tuple[Job, str]:
    s = settings()
    d = json_call(s.openai_model_brain, prompt("job_extract"),
                  json.dumps({"now_ist": _now_ist(), "instruction": text}, ensure_ascii=False), JobDraft, None, "job_extract")
    job = Job(job_id=jobs.new_job_id(), plumber_id=user, created_at=time.time(),
              issue=_fact(d.issue, msg_ref), site_address=_fact(d.site_address, msg_ref), flat=_fact(d.flat, msg_ref),
              access_notes=_fact(d.access_notes, msg_ref))
    contact, phones = _person(d.site_contact)
    job.roles.site_contact = contact
    job.roles.billing_to, _ = _person(d.billing_to)
    if d.billing_to and d.billing_to.email:
        job.roles.billing_to.email = d.billing_to.email
    job.roles.approver, _ = _person(d.approver)
    job.roles.work_order = d.work_order.value
    job.appointment.plumber_available = [Window(start=w.start, end=w.end, label=w.label) for w in d.plumber_windows]
    jobs.log(job, "job_created", f"Job created from the plumber's message: {job.issue.value or 'issue not stated'}", actor="plumber", ref=msg_ref)
    # next action: ask only for what the next step needs (CLAUDE.md §6.1)
    if len(phones) > 1:
        store().put("contacts", f"pendingq:{user}", {"owner": user, "job_id": job.job_id, "field": "site_contact.phone", "options": phones})
        job.next_action = NextAction(what="Choose which number is the customer's", owner="plumber", blocked_by="several numbers")
        ask = "Which number should I call for the customer? " + " or ".join(f"…{p[-4:]}" for p in phones)
    elif not contact or not contact.phone:
        job.next_action = NextAction(what="Customer phone number needed to book the visit", owner="plumber", blocked_by="no customer number")
        ask = "What is the customer's phone number?"
    elif not job.appointment.plumber_available:
        job.next_action = NextAction(what="Your free time windows needed to book the visit", owner="plumber", blocked_by="no windows")
        ask = "When are you free to visit? (e.g. tomorrow 11–1 or after 4)"
    else:
        job.next_action = NextAction(what=f"Call {contact.name or 'the customer'} ({mask_phone(contact.phone)}) to book a visit", owner="chinku")
        ask = f"Shall I call {contact.name or 'the customer'} to book the visit in your windows?"
    jobs.save(job)
    return job, ask


def _answer_pending_question(user: str, text: str) -> tuple[Job, str] | None:
    q = store().get("contacts", f"pendingq:{user}")
    if not q:
        return None
    opts = q["options"]
    digits = re.sub(r"\D", "", text)
    pick = next((o for o in opts if digits and (o.endswith(digits[-4:]) or digits.endswith(o[-10:]))), None)
    low = text.lower()
    if not pick and ("first" in low or "1st" in low):
        pick = opts[0]
    if not pick and ("second" in low or "2nd" in low) and len(opts) > 1:
        pick = opts[1]
    if not pick:
        return None
    job = jobs.get(q["job_id"])
    if not job:
        return None
    job.roles.site_contact = job.roles.site_contact or Person()
    job.roles.site_contact.phone = pick
    jobs.log(job, "detail_corrected", f"Customer number set to …{pick[-4:]}", actor="plumber")
    job.next_action = NextAction(what=f"Call {job.roles.site_contact.name or 'the customer'} ({mask_phone(pick)}) to book a visit", owner="chinku")
    jobs.save(job)
    store().put("contacts", f"pendingq:{user}", {"owner": user, "kind": "done"})
    return job, f"Got it — customer number …{pick[-4:]}. Shall I call to book the visit?"


def _summary_line(job: Job) -> str:
    na = job.next_action.what if job.next_action else "—"
    return f"{job.job_id} · {job.issue.value or '?'} · {job.flat.value or job.site_address.value or '?'} · next: {na}"


CORRECTABLE = {"issue": "issue", "flat": "flat", "site_address": "site_address", "address": "site_address", "access_notes": "access_notes"}


def apply_correction(job: Job, ent: dict[str, Any], user: str) -> str:
    """Voice/text correction updates ONLY that field (CLAUDE.md §3A step 5)."""
    field, new = str(ent.get("field", "")).lower().replace(" ", "_"), ent.get("new_value")
    if new in (None, ""):
        return "What should it be changed to?"
    if field in CORRECTABLE:
        setattr(job, CORRECTABLE[field], Fact(value=new, status="confirmed", evidence_quote=str(new), source="correction"))
    elif field in ("work_order", "wo"):
        job.roles.work_order = str(new)
    elif "phone" in field or "number" in field:
        ph = indian_mobile(new)
        if not ph:
            return f"{new} doesn't look like a mobile number — please send it again."
        job.roles.site_contact = job.roles.site_contact or Person()
        job.roles.site_contact.phone = ph
        new = f"…{ph[-4:]}"
    elif "name" in field or "customer" in field:
        job.roles.site_contact = job.roles.site_contact or Person()
        job.roles.site_contact.name = str(new)
    elif "bill" in field:
        job.roles.billing_to = job.roles.billing_to or Person()
        if "@" in str(new):
            job.roles.billing_to.email = str(new)
        else:
            job.roles.billing_to.name = str(new)
    else:
        return f"I can't change '{field}' yet. Which detail should I change?"
    jobs.log(job, "detail_corrected", f"{field} → {new}", actor="plumber")
    jobs.save(job)
    return f"Updated {job.job_id}: {field.replace('_', ' ')} → {new}."


def _log_understanding(job: Job, heard: Any, readback: str) -> None:
    if heard is not None and getattr(heard, "source", "text") != "text":
        jobs.log(job, "heard", f"Heard ({heard.source}): {heard.text[:200]}", actor="plumber", channel=heard.source, ref=heard.audio_ref)
    if readback:
        jobs.log(job, "understood", readback)


def _execute(user: str, action: Any, text: str, image: dict[str, Any] | None, send: Send, msg_ref: str, heard: Any,
             readback: str, force_confirm: bool) -> str | None:
    """Run one understood action. Returns a reply line (or None if a card/other message was already sent)."""
    if action.intent == "create_job":
        job, ask = create_job(user, text, msg_ref)
        _log_understanding(job, heard, readback)
        jobs.save(job)
        CARD(user, f"{readback}\n\nCreated {job.job_id}. {ask}" if readback else f"Created {job.job_id}. {ask}", jobs.card(job))
        return None
    if action.intent == "small_talk":
        return "I'm Chinku, your office assistant. Tell me a new job, ask me to book a visit, find a part, get an approval or make an invoice."
    if action.intent == "other":
        runner.handle_message(user, text, image, send, force_confirm=force_confirm)
        return None
    job, err = resolve_job(user, action)
    if err:
        return err
    _log_understanding(job, heard, readback)
    jobs.save(job)
    if action.intent == "status_query":
        CARD(user, _summary_line(job), jobs.card(job))
        return None
    if action.intent == "correct_detail":
        msg = apply_correction(job, action.entities, user)
        CARD(user, msg, jobs.card(job))
        return None
    return f"{job.job_id}: {NEXT_INTENTS.get(action.intent, action.intent)} is being built right now — not live yet."


def handle(user: str, text: str, image: dict[str, Any] | None, send: Send, force_confirm: bool = False,
           msg_ref: str | None = None, heard: Any = None) -> None:
    """Every channel lands here (CLAUDE.md §3A): understand → gate → act; the reply starts with the readback."""
    from app.brain import understand as U
    msg_ref = msg_ref or f"msg:{int(time.time())}"
    text = text or ""
    # 1) an engine task (call/email) is waiting for yes/no → the v1 engine owns that gate
    pending = runner.open_task(user)
    if pending and pending["status"] == "awaiting_confirm" and not image and (runner.YES.match(text) or runner.NO.match(text)):
        runner.handle_message(user, text, image, send, force_confirm=force_confirm)
        return
    # 2) a consequential action read back earlier is waiting for yes / no
    pa = store().get("contacts", f"pendingact:{user}")
    if pa and pa.get("actions") and time.time() - pa.get("ts", 0) < 1800 and not image:
        if runner.YES.match(text):
            store().put("contacts", f"pendingact:{user}", {"owner": user, "actions": []})
            from app.brain.schemas import Action
            replies = [r for a in pa["actions"] if (r := _execute(user, Action.model_validate(a), pa["text"], None, send,
                                                                  msg_ref, None, pa["readback"], force_confirm))]
            if replies:
                send("\n".join(replies))
            return
        if runner.NO.match(text):
            store().put("contacts", f"pendingact:{user}", {"owner": user, "actions": []})
            send("Okay, I won't do that.")
            return
    # 3) answer to a question Chinku asked about a job (e.g. which number)
    ans = _answer_pending_question(user, text)
    if ans:
        job, reply = ans
        CARD(user, reply, jobs.card(job))
        return
    # 4) normalise + understand + gate
    norm, res = U.understand(user, text, image)
    g = U.gate(res)
    store().add_event("-", "gate", {"decision": g, "readback": res.readback})
    if g == "ask":
        send(res.clarifying_question or (f"{res.readback}\nDid I get that right?" if res.readback else "Sorry, could you say that again?"))
        return
    if g == "confirm":
        store().put("contacts", f"pendingact:{user}", {"owner": user, "actions": [a.model_dump() for a in res.actions],
                                                         "readback": res.readback, "text": text, "ts": time.time()})
        send(f"{res.readback} — shall I go ahead? (yes / no)")
        return
    replies = []
    for a in res.actions:
        r = _execute(user, a, text, image, send, msg_ref, heard, res.readback, force_confirm)
        if r:
            replies.append(r)
    if replies:
        lead = res.readback if res.readback and len(res.actions) > 0 and res.actions[0].intent not in ("small_talk",) else ""
        send("\n".join(([lead] if lead else []) + replies))
