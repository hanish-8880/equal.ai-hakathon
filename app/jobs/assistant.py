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


def handle(user: str, text: str, image: dict[str, Any] | None, send: Send, force_confirm: bool = False,
           msg_ref: str | None = None) -> None:
    msg_ref = msg_ref or f"msg:{int(time.time())}"
    # 1) an engine task (call/email) is waiting for yes/no/correction → the v1 engine owns that gate
    pending = runner.open_task(user)
    if pending and pending["status"] == "awaiting_confirm" and not image and (runner.YES.match(text or "") or runner.NO.match(text or "")):
        runner.handle_message(user, text, image, send, force_confirm=force_confirm)
        return
    # 2) answer to a question Chinku asked about a job (e.g. which number)
    ans = _answer_pending_question(user, text or "")
    if ans:
        job, reply = ans
        CARD(user, reply, jobs.card(job))
        return
    r = route(user, text or "", image)
    store().add_event("-", "plumber_routed", r.model_dump())
    if r.intent == "create_job":
        job, ask = create_job(user, text or "", msg_ref)
        CARD(user, f"Created {job.job_id}: {job.issue.value or 'new job'}. {ask}", jobs.card(job))
        return
    if r.intent == "status_query":
        if r.job_ref:
            job, err = resolve_job(user, r)
            if err:
                send(err)
                return
            CARD(user, _summary_line(job), jobs.card(job))
            return
        open_ = jobs.open_jobs(user)
        send("Open jobs:\n" + "\n".join(_summary_line(j) for j in open_) if open_ else "No open jobs.")
        return
    if r.intent == "small_talk":
        send(r.clarifying_question or "I'm Chinku, your office assistant. Tell me a new job, ask me to book a visit, find a part, "
                                       "get an approval or make an invoice.")
        return
    if r.intent in NEXT_INTENTS:
        job, err = resolve_job(user, r)
        if err:
            send(err)
            return
        send(f"{job.job_id}: {NEXT_INTENTS[r.intent]} is being built right now — not live yet.")
        return
    # 3) not a plumber-office task → v1 features (dealer quotes, warranty, PDF report, ...)
    runner.handle_message(user, text, image, send, force_confirm=force_confirm)
