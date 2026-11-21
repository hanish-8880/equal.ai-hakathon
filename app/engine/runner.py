"""Spec runner + state machine. No per-feature logic here. Ever.

Task: planned -> awaiting_confirm -> running -> waiting_input -> done | failed | cancelled
Call: queued -> dispatched -> completed | no_answer | failed
"""
import re
import time
import uuid
from typing import Any

from app.brain import router
from app.brain.schemas import RouteResult
from app.engine import steps
from app.engine.specs_loader import catalog, load_specs
from app.engine.validate import VALIDATORS, mask_phone
from app.store.dynamo import store

YES = re.compile(r"^\s*(yes|y|ok|okay|avunu|haa|ha|sare|cheyyi|confirm|go|done|👍)\b", re.I)
NO = re.compile(r"^\s*(no|vaddu|cancel|stop|ledu)\b", re.I)
OPEN = ("planned", "awaiting_confirm", "running", "waiting_input")
EMAIL_IN_TEXT = re.compile(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}")
CONTACT_CMD = re.compile(r"^\s*(?:save\s+)?contact\s+([A-Za-zఀ-౿]+)\s+(.+?)\s*$", re.I)
MEDIA_MEMORY_S = {"document": 3600}  # PDFs remembered 60 min (user may call later); photos 15 min


def now() -> float:
    return time.time()


def save_task(t: dict[str, Any], kind: str = "task_update") -> None:
    store().put("tasks", t["task_id"], t)
    store().add_event(t["task_id"], kind, {"status": t["status"], "step_idx": t.get("step_idx")})


def open_task(user: str) -> dict[str, Any] | None:
    ts = [t for t in store().scan("tasks", user=user) if t["status"] in ("planned", "awaiting_confirm")]
    return max(ts, key=lambda t: t["created_at"]) if ts else None


# ---------------- inbound ----------------
def handle_message(user: str, text: str, image: dict[str, Any] | None, send, force_confirm: bool = False) -> None:
    """Entry point for every WhatsApp message (already perceived). `send(text)` replies to the user."""
    m = CONTACT_CMD.match(text or "")
    if m:
        alias, val = m.group(1).lower(), m.group(2)
        phone, mail = VALIDATORS["indian_mobile"](val), VALIDATORS["email"](val)
        if not (phone or mail):
            send("That number (10 digits) or email doesn't look right — please send it again.")
            return
        key = f"{user}:{alias}"
        c = store().get("contacts", key) or {"user": user, "alias": alias, "relation": alias}
        c.update({"phone": phone} if phone else {"email": mail})
        store().put("contacts", key, c)
        send(f"Saved ✅ {alias} ({mask_phone(phone) if phone else mail})")
        return

    waiting = steps.find_waiting(user, bool(image))
    if waiting and image:  # a contact sent the input a wait_for step needs
        task, sid = waiting
        task["steps"][sid] = {**task["steps"][sid], "status": "done", "output": {"image": image}}
        task["status"] = "running"
        save_task(task, "wait_input_received")
        send("Thank you 🙏 I got the photo. I'll call you shortly and explain it.")
        owner = task["user"]
        advance(task["task_id"], lambda msg: steps.NOTIFY(owner, msg, None))
        return

    pending = open_task(user)
    if pending and pending["status"] == "awaiting_confirm" and not image:
        gate = next((sid for sid, st in pending["steps"].items() if st.get("status") == "awaiting_confirm"), None)
        addr = EMAIL_IN_TEXT.search(text or "")
        if gate and addr:  # correction of the email address during a step gate
            st = pending["steps"][gate]
            st["to"], st["source"] = addr.group(0), "you"
            save_task(pending, "gate_corrected")
            send(steps.email_preview(st))
            return
        if YES.match(text or ""):
            if gate:
                pending["steps"][gate]["status"] = "confirmed"
                pending["status"] = "running"
                save_task(pending, "gate_confirmed")
                advance(pending["task_id"], send)
            else:
                start(pending, send)  # no separate "starting" message (saves WhatsApp quota)
            return
        if NO.match(text or ""):
            pending["status"] = "cancelled"
            save_task(pending)
            send("Okay, cancelled.")
            return
        if gate:  # a started task is never re-planned; show what it is waiting for
            send(steps.email_preview(pending["steps"][gate]) if pending["steps"][gate].get("draft")
                 else "Please reply yes or no first.")
            return

    # a photo sent alone is remembered for ~15 min and used with the next instruction
    if image and not (text or "").strip():
        store().put("contacts", f"lastimg:{user}", {"owner": user, "kind": "last_image", "image": image, "ts": now()})
    elif not image:
        last = store().get("contacts", f"lastimg:{user}")
        window = MEDIA_MEMORY_S.get((last or {}).get("image", {}).get("image_type"), 900)
        if last and now() - last["ts"] < window:
            image = last["image"]
            store().put("contacts", f"lastimg:{user}", {**last, "ts": 0})

    contacts = store().scan("contacts", user=user)
    msg = text or ""
    if pending:  # let the router merge corrections / answers into the pending task
        msg = f"{msg}\n\n[PENDING TASK spec={pending['spec']} inputs={pending['inputs']}]"
    r = router.route(msg, image, catalog(), contacts, pending["task_id"] if pending else None)
    if not r.spec or r.spec not in load_specs():
        if image and not (text or "").strip():
            f = image.get("fields") or {}
            seen = " ".join(str(f[k]) for k in ("brand", "model", "product_type", "project_name") if f.get(k)) or image.get("image_type", "photo")
            send(f"Got the photo 👀 ({seen}). What should I do with it? (send a text or voice note)")
            return
        send(r.clarifying_question or "Nenu phone calls tho errands chestanu — e.g. dealers ki call chesi best rate. Em cheyyali?")
        return

    if pending and pending["spec"] == r.spec:
        task = pending
        task["inputs"] = {**task["inputs"], **{k: v for k, v in r.inputs.items() if v not in (None, "", [])}}
    else:
        if pending:
            pending["status"] = "cancelled"
            save_task(pending)
        task = {"task_id": "t_" + uuid.uuid4().hex[:8], "user": user, "spec": r.spec, "status": "planned",
                "inputs": r.inputs, "language": r.language, "step_idx": 0, "steps": {}, "created_at": now()}
    # media inputs are filled deterministically from what was perceived (the router never copies documents)
    for name, cfg in load_specs()[task["spec"]]["inputs"].items():
        if cfg.get("media") and image and cfg["media"] in (image.get("image_type"), "any"):
            task["inputs"][name] = image
        if cfg.get("source") == "media_contacts" and image:
            found = (image.get("fields") or {}).get("contacts") or image.get("contacts") or []
            if found:
                task["inputs"][name] = [{"label": c.get("label") or f"Number {i + 1}", "phone": c.get("phone"),
                                         "role": c.get("role") or c.get("label") or "contact"} for i, c in enumerate(found)]
    task["force_confirm"] = force_confirm or task.get("force_confirm", False)
    store().add_event(task["task_id"], "routed", {**r.model_dump(), "inputs": {k: ("<media>" if isinstance(v, dict) and v.get("image_type") else v) for k, v in r.inputs.items()}})
    plan(task, r, send)


def normalise_inputs(spec: dict[str, Any], inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply defaults + validators. Returns (clean_inputs, problems)."""
    out, problems = dict(inputs), []
    for name, cfg in spec["inputs"].items():
        v = out.get(name)
        if v in (None, "", []) and "default" in cfg:
            v = out[name] = cfg["default"]
        if cfg.get("required") and v in (None, "", []):
            problems.append(f"missing:{name}")
            continue
        if cfg.get("validate") and v not in (None, ""):
            ok = VALIDATORS[cfg["validate"]](v)
            if ok is None:
                problems.append(f"invalid:{name}")
            else:
                out[name] = ok
        if cfg.get("validate_item") and isinstance(v, list):
            clean, skipped = [], []
            for it in v:
                it = dict(it)
                bad = False
                for f, val_name in cfg["validate_item"].items():
                    ok = VALIDATORS[val_name](it.get(f))
                    if ok is None:
                        bad = True
                        label = it.get("name") or it.get("label") or it.get(f)
                        (skipped if cfg.get("source") == "media_contacts" else problems).append(
                            f"{label} ({it.get(f)})" if cfg.get("source") == "media_contacts" else f"invalid:{name}.{f}:{label}")
                    it[f] = ok
                if not (bad and cfg.get("source") == "media_contacts"):
                    clean.append(it)
            out[name] = clean
            if skipped:  # numbers read from a document that cannot be called (landline, partial) are skipped, not blocking
                out[f"_skipped_{name}"] = skipped
            if cfg.get("required") and not clean:
                problems.append(f"missing:{name}")
        if cfg.get("type") == "number" and isinstance(out.get(name), str):
            try:
                out[name] = float(re.sub(r"[^\d.]", "", out[name]))
            except ValueError:
                problems.append(f"invalid:{name}")
    return out, problems


def plan(task: dict[str, Any], r: RouteResult, send) -> None:
    spec = load_specs()[task["spec"]]
    task["inputs"], problems = normalise_inputs(spec, task["inputs"])
    task["problems"] = problems
    if problems:
        task["status"] = "planned"
        save_task(task)
        q = r.clarifying_question or _problem_question(problems)
        send(q)
        return
    if "call" in spec.get("confirm_before", []):
        task["status"] = "awaiting_confirm"
        save_task(task)
        prefix = "📞 From your call, I understood:\n" if task.get("force_confirm") else ""
        send(prefix + _confirm_text(spec, task))
        return
    if task.get("force_confirm"):  # tasks taken over a phone call always get a WhatsApp yes first
        task["status"] = "awaiting_confirm"
        save_task(task)
        shown = {k: v for k, v in task["inputs"].items()
                 if not k.startswith("_") and not (isinstance(v, dict) and v.get("image_type")) and v not in (None, "", [])}
        details = "\n".join(f"• {k}: {v}" for k, v in shown.items())
        send(f"📞 From your call, I understood: {spec['description'].split(';')[0]}\n{details}\nShall I start? (yes / no)")
        return
    start(task, send)


def _problem_question(problems: list[str]) -> str:
    p = problems[0]
    if "phone" in p:
        return "I couldn't read the phone number clearly — please type it (10 digits)."
    return f"I need a bit more info: {', '.join(x.split(':', 1)[1] for x in problems)}. Could you tell me?"


def _confirm_text(spec: dict[str, Any], task: dict[str, Any]) -> str:
    targets = steps.call_targets(spec, task)
    who = "\n".join(f"• {t['callee'].get('name') or t['callee'].get('role')} ({mask_phone(t['to'])})" for t in targets)
    first = next(s for s in spec["steps"] if s["type"] == "call")
    from app.engine.specs_loader import render
    goal = render(first.get("plan_text") or first["goal"], {"inputs": task["inputs"]})
    if first.get("must_collect_from"):
        _, _, qmap = steps.resolve_collect(first, spec, task)
        goal += "".join(f"\n  ? {q}" for q in qmap.values())
    skipped = [s for k, v in task["inputs"].items() if k.startswith("_skipped_") for s in v]
    skip = ("\nSkipping (can't call): " + ", ".join(skipped)) if skipped else ""
    return f"Plan: {goal}\nCalls:\n{who}{skip}\nShall I call? (yes / no)"


# ---------------- execution ----------------
def start(task: dict[str, Any], send) -> None:
    task["status"] = "running"
    save_task(task, "task_started")
    advance(task["task_id"], send)


def advance(task_id: str, send) -> None:
    """Run steps from step_idx until one has to wait (a call) or the task ends."""
    task = store().get("tasks", task_id)
    if not task or task["status"] not in ("running", "waiting_input"):
        return
    spec = load_specs()[task["spec"]]
    while task["step_idx"] < len(spec["steps"]):
        step = spec["steps"][task["step_idx"]]
        state = task["steps"].get(step["id"], {}).get("status")
        if state == "done":
            task["step_idx"] += 1
            save_task(task)
            continue
        if state in ("running", "awaiting_confirm", "waiting", "scheduled"):
            return  # waiting on webhooks / user / a contact's WhatsApp input
        result = steps.run(step, spec, task, send)
        if result["status"] == "running":
            return  # run_call already stored "running"; webhooks / late failures finish it
        task = store().get("tasks", task_id)  # step may have written
        task["steps"][step["id"]] = result
        if result["status"] == "awaiting_confirm":
            task["status"] = "awaiting_confirm"
        if result["status"] == "waiting":
            task["status"] = "waiting_input"
        if result["status"] == "failed":
            task["status"] = "failed"
        save_task(task, f"step_{result['status']}")
        if result["status"] != "done":
            return
        task["step_idx"] += 1
    task["status"] = "done"
    save_task(task, "task_done")


def on_call_result(parsed: dict[str, Any], send_to_user) -> None:
    """Omni webhook (already deduped). Extract, then advance task when the step's calls are all terminal."""
    call = steps.record_call_result(parsed)
    if not call:
        return
    task = store().get("tasks", call["task_id"])
    if not task:
        return
    spec = load_specs()[task["spec"]]
    step = next(s for s in spec["steps"] if s["id"] == call["step_id"])
    if steps.maybe_retry(call, step, task):
        return
    finish_call_step(task["task_id"], step["id"], lambda m: send_to_user(task["user"], m))


def finish_call_step(task_id: str, step_id: str, send) -> None:
    task = store().get("tasks", task_id)
    if not task or task["steps"].get(step_id, {}).get("status") == "done":
        return
    if steps.step_calls_terminal(task_id, step_id):
        task["steps"][step_id] = {"status": "done", "output": steps.collect(task_id, step_id)}
        save_task(task, "step_done")
        advance(task_id, send)


def _late_terminal(task_id: str, step_id: str) -> None:
    t = store().get("tasks", task_id)
    if t:
        finish_call_step(task_id, step_id, lambda m: steps.NOTIFY(t["user"], m, None))


steps.ON_CALL_TERMINAL = _late_terminal


def fire_schedule(task_id: str, step_id: str) -> bool:
    """Fire one scheduled step (from the tick loop, or POST /internal/fire for EventBridge). Idempotent."""
    if not store().claim(f"fire:{task_id}:{step_id}"):
        return False
    task = store().get("tasks", task_id)
    if not task or task["steps"].get(step_id, {}).get("status") != "scheduled":
        return False
    task["steps"][step_id] = {**task["steps"][step_id], "status": "done", "output": {"fired_at": now()}}
    save_task(task, "schedule_fired")
    owner = task["user"]
    advance(task_id, lambda m: steps.NOTIFY(owner, m, None))
    return True


def tick_schedules() -> int:
    n = 0
    for task_id, step_id in steps.due_schedules():
        n += fire_schedule(task_id, step_id)
    return n
