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


def now() -> float:
    return time.time()


def save_task(t: dict[str, Any], kind: str = "task_update") -> None:
    store().put("tasks", t["task_id"], t)
    store().add_event(t["task_id"], kind, {"status": t["status"], "step_idx": t.get("step_idx")})


def open_task(user: str) -> dict[str, Any] | None:
    ts = [t for t in store().scan("tasks", user=user) if t["status"] in ("planned", "awaiting_confirm")]
    return max(ts, key=lambda t: t["created_at"]) if ts else None


# ---------------- inbound ----------------
def handle_message(user: str, text: str, image: dict[str, Any] | None, send) -> None:
    """Entry point for every WhatsApp message (already perceived). `send(text)` replies to the user."""
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
                send("Sare 👍 calls start chestunna. Result WhatsApp lo pamputa.")
                start(pending, send)
            return
        if NO.match(text or ""):
            pending["status"] = "cancelled"
            save_task(pending)
            send("Ok, cancel chesa.")
            return

    # a photo sent alone is remembered for ~15 min and used with the next instruction
    if image and not (text or "").strip():
        store().put("contacts", f"lastimg:{user}", {"owner": user, "kind": "last_image", "image": image, "ts": now()})
    elif not image:
        last = store().get("contacts", f"lastimg:{user}")
        if last and now() - last["ts"] < 900:
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
            send(f"Photo chusa 👀 ({seen}). Deeni gurinchi em cheyyali? (voice note / text pampandi)")
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
    store().add_event(task["task_id"], "routed", r.model_dump())
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
            clean = []
            for it in v:
                it = dict(it)
                for f, val_name in cfg["validate_item"].items():
                    ok = VALIDATORS[val_name](it.get(f))
                    if ok is None:
                        problems.append(f"invalid:{name}.{f}:{it.get('name') or it.get(f)}")
                    it[f] = ok
                clean.append(it)
            out[name] = clean
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
        send(_confirm_text(spec, task))
        return
    start(task, send)


def _problem_question(problems: list[str]) -> str:
    p = problems[0]
    if "phone" in p:
        return "Phone number sariga kanipinchatledu — number type chesi pampandi (10 digits)."
    return f"Inka konchem info kavali: {', '.join(x.split(':', 1)[1] for x in problems)}. Cheppandi?"


def _confirm_text(spec: dict[str, Any], task: dict[str, Any]) -> str:
    targets = steps.call_targets(spec, task)
    who = "\n".join(f"• {t['callee'].get('name') or t['callee'].get('role')} ({mask_phone(t['to'])})" for t in targets)
    first = next(s for s in spec["steps"] if s["type"] == "call")
    from app.engine.specs_loader import render
    goal = render(first["goal"], {"inputs": task["inputs"]})
    return f"Plan: {goal}\nCalls:\n{who}\nCall cheyyana? (yes / no)"


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
        if state in ("running", "awaiting_confirm"):
            return  # waiting on webhooks / user
        result = steps.run(step, spec, task, send)
        task = store().get("tasks", task_id)  # step may have written
        task["steps"][step["id"]] = result
        if result["status"] == "awaiting_confirm":
            task["status"] = "awaiting_confirm"
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
    if steps.step_calls_terminal(task["task_id"], step["id"]):
        task["steps"][step["id"]] = {"status": "done", "output": steps.collect(task["task_id"], step["id"])}
        save_task(task, "step_done")
        advance(task["task_id"], lambda m: send_to_user(task["user"], m))
