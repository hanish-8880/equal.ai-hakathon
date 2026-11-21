"""Deterministic job operations (`job_op` step type). Code owns exactness: slot locking, state, evidence links.
Each op: (step, spec, task, send) -> step result {"status": ..., "output": {...}}."""
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.engine.validate import mask_phone
from app.jobs import store as jobs
from app.jobs.models import Fact, NextAction, Window
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
CARD: Callable[[str, str, dict[str, Any]], None] = lambda to, text, card: None   # set by main.py
NOTIFY: Callable[[str, str, Any], None] = lambda to, text, audio=None: None      # set by main.py


def wa_joined(phone: str | None) -> bool:
    """Sandbox reaches only joined numbers: the owner (he chats with Chinku) or anyone who has messaged the sandbox."""
    from app.config import settings
    from app.engine.validate import indian_mobile
    if not phone:
        return False
    owner = indian_mobile(settings().user_whatsapp.replace("whatsapp:", ""))
    return phone == owner or store().get("contacts", f"wajoined:{phone}") is not None


def slot_key(plumber: str, start_iso: str) -> str:
    return f"slot:{plumber}:{datetime.fromisoformat(start_iso).astimezone(IST).isoformat()}"


def slot_taken(plumber: str, start_iso: str) -> bool:
    return store().get("seen", slot_key(plumber, start_iso)) is not None


def free_windows(plumber: str, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live availability: windows not already locked by another booking and not in the past."""
    now = datetime.now(IST)
    out = []
    for w in windows:
        try:
            if datetime.fromisoformat(w["end"]) <= now:
                continue
        except (KeyError, ValueError):
            continue
        if not slot_taken(plumber, w["start"]):
            out.append(w)
    return out


def _call_fields(task: dict[str, Any], step_id: str) -> dict[str, Any]:
    out = (task["steps"].get(step_id) or {}).get("output") or []
    rows = out if isinstance(out, list) else [out]
    for r in rows:
        if (r or {}).get("status") == "completed":
            return r.get("fields") or {}
    return {}


def _val(f: dict[str, Any] | None) -> Any:
    return f.get("value") if f and f.get("status") in ("confirmed", "tentative") else None


def pick_window(chosen: Any, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map what the customer chose (ISO time or window label) onto one of the offered windows."""
    if not chosen:
        return None
    s = str(chosen)
    try:
        t = datetime.fromisoformat(s)
        t = t if t.tzinfo else t.replace(tzinfo=IST)
        for w in windows:
            if datetime.fromisoformat(w["start"]) <= t < datetime.fromisoformat(w["end"]):
                return w
    except ValueError:
        pass
    low = s.lower()
    return next((w for w in windows if (w.get("label") or "").lower() in low or low in (w.get("label") or "").lower()), None)


def book_slot(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    job = jobs.get(task["inputs"]["job_id"])
    if not job:
        send("I couldn't find that job any more.")
        return {"status": "failed", "error": "job missing"}
    f = _call_fields(task, step.get("from_step", "call_client"))
    call_id = next((c["call_id"] for c in store().scan("calls", task_id=task["task_id"], step_id=step.get("from_step", "call_client"))
                    if c["status"] == "completed"), None)
    offered = task["inputs"].get("windows") or []
    chosen = _val(f.get("chosen_slot"))
    w = pick_window(chosen, offered)
    # record the facts the customer gave, each with its evidence quote and the call that produced it
    for name, attr in (("address", "site_address"), ("flat", "flat"), ("access_notes", "access_notes")):
        fx = f.get(name) or {}
        if fx.get("value") and fx.get("status") in ("confirmed", "tentative"):
            setattr(job, attr, Fact(value=fx["value"], status=fx["status"], evidence_quote=fx.get("evidence_quote"), source=call_id))
    who = _val(f.get("present_person"))
    if who and job.roles.site_contact:
        job.roles.site_contact.name = job.roles.site_contact.name or str(who)
    job.appointment.requested_by_client = Fact(value=chosen, status=(f.get("chosen_slot") or {}).get("status", "not_mentioned"),
                                               evidence_quote=(f.get("chosen_slot") or {}).get("evidence_quote"), source=call_id)
    if not w:
        job.appointment.state = "pending"
        job.next_action = NextAction(what="Customer wants a different time" if chosen else "Customer did not pick a time",
                                     owner="plumber", blocked_by=f"asked for: {chosen or 'nothing clear'}")
        jobs.log(job, "booking_pending", f"No offered window matched (customer said: {chosen or '—'})", actor="client", channel="call", ref=call_id)
        jobs.save(job)
        CARD(task["user"], f"{job.job_id}: the customer didn't take your windows (said: {chosen or 'nothing clear'}). Which time should I offer?", jobs.card(job))
        return {"status": "stop", "output": {"booked": False, "chosen": chosen}}
    # lock: conditional write — two customers can never take one slot
    if not store().claim(slot_key(job.plumber_id, w["start"])):
        nxt = free_windows(job.plumber_id, offered)
        job.appointment.state = "proposed"
        job.next_action = NextAction(what=f"Slot {w.get('label')} was just taken — offer {nxt[0]['label'] if nxt else 'another time'}?",
                                     owner="plumber", blocked_by="slot taken")
        jobs.log(job, "slot_conflict", f"{w.get('label')} was taken by another booking at save time", channel="code", ref=call_id)
        jobs.save(job)
        CARD(task["user"], f"{job.job_id}: {w.get('label')} was just booked by another job. Offer {nxt[0]['label'] if nxt else 'a new time'} instead?",
             jobs.card(job))
        return {"status": "stop", "output": {"booked": False, "conflict": w}}
    job.appointment.slot = Window(**{k: w[k] for k in ("start", "end", "label") if k in w})
    job.appointment.state = "booked"
    job.appointment.confirmed_both = True
    job.appointment.history.append({"ts": time.time(), "event": "booked", "slot": w, "ref": call_id})
    job.status = "booked"
    job.next_action = NextAction(what=f"Visit {w.get('label')} · confirmation call 1 hour before", owner="chinku")
    jobs.log(job, "visit_booked", f"Visit booked {w.get('label')} (customer: “{(f.get('chosen_slot') or {}).get('evidence_quote') or chosen}”)",
             actor="client", channel="call", ref=call_id)
    jobs.save(job)
    CARD(task["user"], f"Booked ✅ {job.job_id} · {w.get('label')} · {job.flat.value or job.site_address.value or ''}", jobs.card(job))
    return {"status": "done", "output": {"booked": True, "fields": {"slot_start": {"value": w["start"], "status": "confirmed"}}, "slot": w}}


def notify_client(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    """Confirmation to the customer on WhatsApp if they joined the sandbox; else record that a call is the channel."""
    job = jobs.get(task["inputs"]["job_id"])
    if not job or not job.appointment.slot or not job.roles.site_contact or not job.roles.site_contact.phone:
        return {"status": "done", "output": {"sent": False, "reason": "nothing booked"}}
    phone = job.roles.site_contact.phone
    joined = wa_joined(phone)
    text = (f"Hello{(' ' + job.roles.site_contact.name) if job.roles.site_contact.name else ''}, this is Chinku, assistant of plumber "
            f"{task['inputs'].get('plumber_name', '')}. Your visit is booked: {job.appointment.slot.label} at "
            f"{job.flat.value or ''} {job.site_address.value or ''}. Reply here if anything changes.")
    if joined:
        NOTIFY(f"whatsapp:{phone}", text, None)
        jobs.log(job, "client_notified", f"WhatsApp confirmation sent to {mask_phone(phone)}", channel="whatsapp")
    else:
        jobs.log(job, "client_notified", f"{mask_phone(phone)} has not joined WhatsApp — confirmed on the call; reminder call before the visit",
                 channel="call")
    jobs.save(job)
    return {"status": "done", "output": {"sent": joined, "channel": "whatsapp" if joined else "call"}}


def visit_confirmed(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    job = jobs.get(task["inputs"]["job_id"])
    f = _call_fields(task, step.get("from_step", "confirm_call"))
    ok = _val(f.get("still_ok"))
    if job:
        if ok is False or str(ok).lower() in ("no", "false"):
            job.appointment.state = "pending"
            job.next_action = NextAction(what="Customer can't make the booked time — reschedule?", owner="plumber", blocked_by="time not OK")
        else:
            job.next_action = NextAction(what=f"Go for the visit {job.appointment.slot.label if job.appointment.slot else ''}", owner="plumber")
        jobs.log(job, "visit_confirm_call", f"Confirmation call: time OK = {ok}", actor="client", channel="call")
        jobs.save(job)
        CARD(task["user"], f"{job.job_id}: confirmation call done — {'time is fine' if ok not in (False, 'no', 'false') else 'customer wants another time'}.",
             jobs.card(job))
    return {"status": "done", "output": {"still_ok": ok}}


OPS = {"book_slot": book_slot, "notify_client": notify_client, "visit_confirmed": visit_confirmed}


def run(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    from app.jobs import parts  # noqa: F401  (registers record_offers / compare_offers)
    return OPS[step["op"]](step, spec, task, send)
