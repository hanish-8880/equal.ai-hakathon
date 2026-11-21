"""The ONLY step types: call | wait_for | email | schedule | reply."""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.actions import email as email_action
from app.brain import brief_writer, decider, extractor, lookup, perceive
from app.config import settings
from app.engine import validate
from app.engine.specs_loader import arith, render
from app.store.dynamo import store
from app.voice import omni

IST = timezone(timedelta(hours=5, minutes=30))
TERMINAL = {"completed", "no_answer", "failed"}
RETRY_DELAY_S = 300
DISPATCH = omni.dispatch_call  # swapped in tests
QUEUE_POLL_S, QUEUE_MAX_S, JOIN_S = 15, 600, 45
ON_CALL_TERMINAL = lambda task_id, step_id: None  # set by runner
# SEND_FILE(to_whatsapp, data, filename, caption): send a document. Set by main.py.
SEND_FILE = lambda to, data, filename, caption: store().add_event("-", "send_file_unwired", {"file": filename})
# NOTIFY(to_whatsapp, text, mp3_bytes_or_None): message anyone (owner or contact). Set by main.py.
NOTIFY = lambda to, text, audio=None: store().add_event("-", "notify_unwired", {"to_last4": to[-4:], "text": text[:200]})


def tctx(task: dict[str, Any], item: Any = None) -> dict[str, Any]:
    return {"inputs": task["inputs"], "steps": task.get("steps", {}), "item": item or {}}


def inr(v: float) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567 (deterministic)."""
    neg, v = v < 0, abs(v)
    whole, frac = int(v), round(v - int(v), 2)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    if frac:
        s += f"{frac:.2f}"[1:]
    return ("-" if neg else "") + s


def _daemon(t: "threading.Timer") -> None:
    t.daemon = True  # never keep the process alive just for a pending reminder/retry
    t.start()


def call_targets(spec: dict[str, Any], task: dict[str, Any], step: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    step = step or next(s for s in spec["steps"] if s["type"] == "call")
    items = task["inputs"].get(step["for_each"], []) if step.get("for_each") else [None]
    out = []
    for it in items:
        ctx = tctx(task, it)
        out.append({"to": render(step["to"], ctx), "callee": render(step.get("callee", {}), ctx), "item": it})
    return out


# ---------------- call ----------------
def _prior_results(task: dict[str, Any]) -> dict[str, Any]:
    return {k: v.get("output") for k, v in task.get("steps", {}).items() if v.get("status") == "done"}


def resolve_collect(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any]) -> tuple[list[str], dict[str, Any], dict[str, str]]:
    """(must_collect fields, result schema for them, field -> question). Static from the spec, or dynamic from task inputs."""
    if step.get("must_collect_from"):
        from app.engine.specs_loader import _lookup
        qs = _lookup(tctx(task), step["must_collect_from"]) or []
        qmap = {q["field"]: q["question"] for q in qs if isinstance(q, dict) and q.get("field")}
        return list(qmap), {f: {"type": "string", "question": q} for f, q in qmap.items()}, qmap
    fields = list(step["must_collect"])
    return fields, {k: spec.get("result_schema", {}).get(k, {}) for k in fields}, {}


def dispatch_one(task: dict[str, Any], step: dict[str, Any], target: dict[str, Any], attempt: int = 0) -> dict[str, Any]:
    cid = "c_" + uuid.uuid4().hex[:10]
    call = {"call_id": cid, "task_id": task["task_id"], "step_id": step["id"], "to_number": target["to"],
            "callee": target["callee"], "attempt": attempt, "status": "queued", "created_at": time.time()}
    store().put("calls", cid, call)
    try:
        from app.engine.specs_loader import load_specs
        fields, _, qmap = resolve_collect(step, load_specs()[task["spec"]], task)
        bstep = {**step, "must_collect": fields, "field_questions": qmap}
        if (task.get("inputs") or {}).get("language"):  # per-call language (callee's language: Telugu for Mummy, Kannada shop…)
            bstep["language"] = task["inputs"]["language"]
        brief = brief_writer.write_brief(bstep, task["inputs"], target["callee"], _prior_results(task), task["task_id"])
        call["brief"] = brief.model_dump()
        ctx = {**{k: (v if isinstance(v, (str, int, float)) else str(v)) for k, v in brief.model_dump().items()
                  if k in ("on_behalf_of", "language", "max_minutes", "call_script", "callee_role")},
               "task_id": task["task_id"], "step_id": step["id"], "chinku_call_id": cid}
        resp = _dispatch_queued(call, target["to"], ctx)
        call["omni"] = resp
        call["omni_call_id"] = str(resp.get("requestId") or resp.get("call_id") or resp.get("id") or "")
        call["status"] = "dispatched"
        store().add_event(task["task_id"], "call_dispatched", {"call_id": cid, "to": target["to"][-4:], "omni": resp})
    except Exception as e:  # noqa: BLE001 — any dispatch failure ends this call as failed
        call["status"] = "failed"
        call["error"] = str(e)[:500]
        store().add_event(task["task_id"], "call_failed", {"call_id": cid, "error": call["error"]})
    store().put("calls", cid, call)
    if call["status"] == "failed" and call.get("waited_in_queue"):
        ON_CALL_TERMINAL(task["task_id"], step["id"])  # run_call already returned; finish the step from here
    return call


def _dispatch_queued(call: dict[str, Any], to: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Omni plan allows N concurrent calls (ours: 1). On concurrency_limit, wait in queue and retry."""
    deadline = time.time() + QUEUE_MAX_S
    while True:
        try:
            return DISPATCH(to, ctx)
        except Exception as e:  # noqa: BLE001
            if "concurrency_limit" not in str(e) or time.time() > deadline:
                raise
            if not call.get("waited_in_queue"):
                call["waited_in_queue"] = True
                store().put("calls", call["call_id"], call)
                store().add_event(call["task_id"], "call_queued", {"call_id": call["call_id"], "reason": "omni concurrency limit"})
            time.sleep(QUEUE_POLL_S)


def run_call(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    targets = call_targets(spec, task, step)
    # mark running BEFORE dispatch so fast webhooks see a running step
    t = store().get("tasks", task["task_id"])
    t["steps"][step["id"]] = {"status": "running"}
    store().put("tasks", t["task_id"], t)
    threads = [threading.Thread(target=dispatch_one, args=(task, step, tg)) for tg in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=JOIN_S)  # queued calls keep waiting in their thread
    if step_calls_terminal(task["task_id"], step["id"]):  # e.g. all dispatches failed
        return {"status": "done", "output": collect(task["task_id"], step["id"])}
    return {"status": "running"}


def find_call(parsed: dict[str, Any]) -> dict[str, Any] | None:
    raw_ctx = (parsed.get("raw") or {}).get("call_context") or {}
    cid = parsed.get("chinku_call_id") or raw_ctx.get("chinku_call_id")
    if cid:
        return store().get("calls", cid)
    if parsed.get("call_id"):
        hits = store().scan("calls", omni_call_id=parsed["call_id"])
        if hits:
            return hits[0]
    if parsed.get("task_id") and parsed.get("step_id"):
        hits = [c for c in store().scan("calls", task_id=parsed["task_id"], step_id=parsed["step_id"])
                if c["status"] == "dispatched"]
        if len(hits) == 1:
            return hits[0]
    return None


def record_call_result(parsed: dict[str, Any]) -> dict[str, Any] | None:
    call = find_call(parsed)
    if not call:
        store().add_event("-", "webhook_unmatched", {"call_id": parsed.get("call_id")})
        return None
    if call["status"] in TERMINAL:
        return None  # duplicate after terminal: never re-process
    task = store().get("tasks", call["task_id"])
    from app.engine.specs_loader import load_specs
    spec = load_specs()[task["spec"]]
    step = next(s for s in spec["steps"] if s["id"] == call["step_id"])
    transcript = parsed.get("transcript") or ""
    call["transcript"] = transcript
    if not transcript.strip() or parsed.get("status") in ("no-answer", "busy", "failed", "no_answer"):
        call["status"], call["outcome"] = "no_answer", "no_answer"
    else:
        fields, schema, _ = resolve_collect(step, spec, task)
        ex = extractor.extract(transcript, fields, schema, task["task_id"])
        call["extracted"] = ex.model_dump()
        call["outcome"] = ex.call_outcome
        call["status"] = "no_answer" if ex.call_outcome == "no_answer" else "completed"
    call["duration"] = parsed.get("duration")
    store().put("calls", call["call_id"], call)
    store().add_event(call["task_id"], "call_result", {"call_id": call["call_id"], "outcome": call["outcome"]})
    return call


def maybe_retry(call: dict[str, Any], step: dict[str, Any], task: dict[str, Any]) -> bool:
    if call["status"] != "no_answer" or call["attempt"] >= int(step.get("retry_on_no_answer", 0)):
        return False
    # Persisted retry (survives restarts): the call record itself waits as "retry_scheduled" until retry_at;
    # runner.fire_due_retries() (15 s loop) dispatches it. Until then the step is not terminal.
    call["status"], call["retry_at"] = "retry_scheduled", time.time() + RETRY_DELAY_S
    store().put("calls", call["call_id"], call)
    store().add_event(task["task_id"], "call_retry_scheduled", {"call_id": call["call_id"], "in_s": RETRY_DELAY_S})
    return True


def due_retries(now_ts: float | None = None) -> list[dict[str, Any]]:
    now_ts = now_ts or time.time()
    return [c for c in store().scan("calls", status="retry_scheduled") if c.get("retry_at", 0) <= now_ts]


def fire_retry(call: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch the retry for a call in retry_scheduled (idempotent)."""
    if not store().claim(f"retry:{call['call_id']}"):
        return None
    from app.engine.specs_loader import load_specs
    task = store().get("tasks", call["task_id"])
    if not task:
        return None
    step = next(s for s in load_specs()[task["spec"]]["steps"] if s["id"] == call["step_id"])
    call["status"], call["superseded"] = "no_answer", True
    store().put("calls", call["call_id"], call)
    return dispatch_one(task, step, {"to": call["to_number"], "callee": call["callee"]}, call["attempt"] + 1)


def step_calls_terminal(task_id: str, step_id: str) -> bool:
    calls = [c for c in store().scan("calls", task_id=task_id, step_id=step_id) if not c.get("superseded")]
    return bool(calls) and all(c["status"] in TERMINAL for c in calls)


def collect(task_id: str, step_id: str) -> list[dict[str, Any]]:
    out = []
    for c in sorted(store().scan("calls", task_id=task_id, step_id=step_id), key=lambda c: c["created_at"]):
        if c.get("superseded"):
            continue
        fields = {f["name"]: f for f in (c.get("extracted") or {}).get("fields", [])}
        out.append({"callee": c["callee"], "to_last4": c["to_number"][-4:] if c.get("to_number") else None,
                    "status": c["status"], "outcome": c.get("outcome") or ("dispatch_failed" if c["status"] == "failed" else None),
                    "fields": fields})
    return out


# ---------------- reply ----------------
def rank(results: list[dict[str, Any]], expr: str, inputs: dict[str, Any], schema: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Deterministic comparison. The LLM never computes money."""
    rows = []
    for r in results:
        names = {k: v for k, v in inputs.items() if isinstance(v, (int, float))}
        for k, cfg in schema.items():
            f = r["fields"].get(k) or {}
            v = f.get("value") if f.get("status") in ("confirmed", "tentative") else None
            if v is None and "default" in cfg:
                v = cfg["default"]
            if isinstance(v, str):
                try:
                    v = float(v.replace(",", ""))
                except ValueError:
                    v = None
            names[k] = v
        v = arith(expr, names)
        v = int(v) if v is not None and float(v).is_integer() else v
        rows.append({"callee": r["callee"].get("name") or r["callee"].get("role"), label: v,
                     "tentative": any(f.get("status") == "tentative" for f in r["fields"].values())})
    priced = sorted([x for x in rows if x[label] is not None], key=lambda x: x[label])
    return priced + [x for x in rows if x[label] is None]


def run_reply(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    prior = _prior_results(task)
    results = [r for out in prior.values() if isinstance(out, list) for r in out]
    results += [{"step": k, "output": out} for k, out in prior.items() if isinstance(out, dict)]
    ranking = rank(results, step["rank_by"], task["inputs"], spec.get("result_schema", {}),
                   step.get("rank_label", "score")) if step.get("rank_by") else None
    ctx = tctx(task)
    hint = render(step.get("template_hint", ""), ctx)
    from app import prefs
    lang = prefs.language(task["user"])
    text = decider.compose_reply(hint, task["inputs"], results, ranking, lang, task["task_id"])
    to = render(step["to"], ctx) if step.get("to") else None
    if to:  # message for someone other than the task owner
        wa_to = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        NOTIFY(wa_to, text, perceive.tts(text) if step.get("voice") else None)
        store().add_event(task["task_id"], "reply_to_contact", {"to_last4": to[-4:], "voice": bool(step.get("voice"))})
    else:
        send(text)
    return {"status": "done", "output": {"text": text, "ranking": ranking, "to_last4": to[-4:] if to else None}}


# ---------------- wait_for ----------------
def _same_phone(a: str | None, b: str | None) -> bool:
    return bool(a and b) and validate.indian_mobile(a.replace("whatsapp:", "")) == validate.indian_mobile(b.replace("whatsapp:", ""))


def run_wait_for(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    timeout_s = int(step.get("timeout_min", 120)) * 60
    frm = render(step["from"], {"inputs": task["inputs"]})
    state = {"status": "waiting", "from": frm, "kind": step.get("kind", "image"), "deadline": time.time() + timeout_s}
    _daemon(threading.Timer(timeout_s / 2, _wait_remind, args=(task["task_id"], step)))
    _daemon(threading.Timer(timeout_s, _wait_timeout, args=(task["task_id"], step)))
    store().add_event(task["task_id"], "waiting_for", {"from_last4": frm[-4:], "kind": state["kind"]})
    return state


def _wait_state(task_id: str, step_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    t = store().get("tasks", task_id)
    return t, (t or {}).get("steps", {}).get(step_id, {})


def _wait_remind(task_id: str, step: dict[str, Any]) -> None:
    t, st = _wait_state(task_id, step["id"])
    if t and st.get("status") == "waiting" and step.get("remind_text"):
        NOTIFY(f"whatsapp:{st['from']}", step["remind_text"], None)
        store().add_event(task_id, "wait_reminded", {})


def _wait_timeout(task_id: str, step: dict[str, Any]) -> None:
    t, st = _wait_state(task_id, step["id"])
    if t and st.get("status") == "waiting":
        t["steps"][step["id"]] = {**st, "status": "failed"}
        t["status"] = "failed"
        store().put("tasks", task_id, t)
        store().add_event(task_id, "wait_timeout", {})
        NOTIFY(t["user"], f"No photo came in {step.get('timeout_min', 120)} min, so I stopped the task. Shall I try again?", None)


def find_waiting(sender: str, has_image: bool) -> tuple[dict[str, Any], str] | None:
    """A task waiting for input from this sender (wait_for step)."""
    for t in store().scan("tasks", status="waiting_input"):
        for sid, st in t["steps"].items():
            if st.get("status") == "waiting" and _same_phone(st.get("from"), sender):
                if st.get("kind") == "image" and not has_image:
                    continue
                return t, sid
    return None


# ---------------- compute ----------------
def run_compute(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    """Deterministic arithmetic over a list from inputs/steps. The LLM never computes money."""
    from app.engine.specs_loader import _lookup
    ctx = tctx(task)
    rows = _lookup(ctx, step["list"]) or []
    if not rows and step.get("skip_if_empty"):
        return {"status": "done", "output": {"total": None, "skipped": "no line items"}}
    field = step["field"]
    vals, missing = [], []
    for r in rows:
        v = (r or {}).get(field)
        try:
            vals.append(float(str(v).replace(",", "")))
        except (TypeError, ValueError):
            missing.append((r or {}).get("label") or str(r)[:40])
    if step.get("op", "sum") != "sum":
        raise NotImplementedError(f"compute op {step.get('op')}")
    total = round(sum(vals), 2)
    stated = _lookup(ctx, step["check_against"]) if step.get("check_against") else None
    try:
        stated = float(str(stated).replace(",", "")) if stated not in (None, "") else None
    except ValueError:
        stated = None
    out = {"total": total, "total_str": inr(total), "count": len(vals), "missing": missing,
           "stated_total": stated, "matches_stated": (abs(stated - total) < 0.5) if stated is not None else None}
    store().add_event(task["task_id"], "computed", out)
    return {"status": "done", "output": out}


# ---------------- fetch_email ----------------
def run_fetch_email(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    """Owner's own inbox, read-only: newest mail with a PDF → read the PDF. The raw PDF is not stored."""
    from app.actions import inbox
    ctx = tctx(task)
    frm, subj = render(step.get("from", ""), ctx) or None, render(step.get("subject", ""), ctx) or None
    found = inbox.find_latest_pdf(frm, subj, int(step.get("days", 7)))
    if not found:
        who = f" from {frm}" if frm else ""
        send(f"I couldn't find an email{who} with a PDF in the last {step.get('days', 7)} days. Can you tell me the sender's name or the subject?")
        return {"status": "failed", "error": "no matching mail"}
    doc = perceive.read_document(found["pdf"], found["filename"], task["task_id"]).model_dump()
    meta = {k: found[k] for k in ("from", "from_email", "subject", "date", "filename")}
    store().add_event(task["task_id"], "email_fetched", {**meta, "items": len(doc.get("line_items", []))})
    return {"status": "done", "output": {"email": meta, "document": doc}}


# ---------------- make_doc ----------------
def run_make_doc(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    from app.actions import slides
    from app.engine.specs_loader import _lookup
    ctx = tctx(task)
    when = render(step.get("when", True), ctx)
    if str(when).lower() in ("false", "no", "0", "none", ""):
        return {"status": "done", "output": {"skipped": "not requested"}}
    if step.get("kind", "pptx") != "pptx":
        raise NotImplementedError(f"make_doc kind {step.get('kind')}")
    doc = _lookup(ctx, step["source"]) or {}
    computed = (task["steps"].get("total") or {}).get("output")
    meta = ((task["steps"].get("fetch") or {}).get("output") or {}).get("email")
    req = render(step.get("request", ""), ctx)
    analysis = decider.analyze_document(req, doc, computed, task["task_id"]).model_dump()
    deck = decider.write_deck(req, doc, analysis, computed, meta, task["task_id"]).model_dump()
    table = [(it.get("label", ""), inr(float(it["amount"]))) for it in doc.get("line_items", []) if it.get("amount") is not None]
    total = (computed or {}).get("total_str")
    data = slides.render(deck, table or None, total, footer=f"Prepared by Chinku for {settings().user_name}")
    fname = f"{task['task_id']}_{(deck['title'] or 'report')[:30].replace(' ', '_')}.pptx"
    SEND_FILE(task["user"], data, fname, f"📊 {deck['title']} — PPT ready")
    store().add_event(task["task_id"], "doc_made", {"file": fname, "slides": len(deck.get("slides", [])) + 1 + bool(table)})
    return {"status": "done", "output": {"file": fname, "slides": len(deck.get("slides", [])), "analysis": analysis}}


# ---------------- schedule ----------------
def _field_from_step(task: dict[str, Any], step_id: str, field: str) -> Any:
    out = task.get("steps", {}).get(step_id, {}).get("output")
    rows = out if isinstance(out, list) else [out] if out else []
    for r in rows:
        f = ((r or {}).get("fields") or {}).get(field) or {}
        if f.get("value") not in (None, "") and f.get("status") in ("confirmed", "tentative"):
            return f["value"]
    return None


def run_schedule(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    """Deterministic fire time = datetime from a prior step + offset. Persisted in task state; runner.tick_schedules fires it."""
    src = step["at_from"]
    raw = _field_from_step(task, src["step"], src["field"])
    try:
        at = datetime.fromisoformat(str(raw))
        if at.tzinfo is None:
            at = at.replace(tzinfo=IST)
    except (TypeError, ValueError):
        send(f"I couldn't understand the time ({raw!r}), so I couldn't set the reminder.")
        return {"status": "failed", "error": f"bad datetime {raw!r}"}
    real_fire = at.timestamp() + int(step.get("offset_min", 0)) * 60
    demo = settings().demo_reminder_seconds
    fire_at = time.time() + demo if demo > 0 else max(real_fire, time.time())
    store().add_event(task["task_id"], "scheduled", {"step": step["id"], "real_fire_at": real_fire, "fire_at": fire_at, "demo": demo > 0})
    return {"status": "scheduled", "fire_at": fire_at, "real_fire_at": real_fire, "slot": at.isoformat(), "demo_time": demo > 0}


def due_schedules(now_ts: float | None = None) -> list[tuple[str, str]]:
    now_ts = now_ts or time.time()
    out = []
    for t in store().scan("tasks", status="running"):
        for sid, st in t["steps"].items():
            if st.get("status") == "scheduled" and st.get("fire_at", 0) <= now_ts:
                out.append((t["task_id"], sid))
    return out


def run(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    if step["type"] == "schedule":
        return run_schedule(step, spec, task, send)
    if step["type"] == "compute":
        return run_compute(step, spec, task, send)
    if step["type"] == "fetch_email":
        return run_fetch_email(step, spec, task, send)
    if step["type"] == "make_doc":
        return run_make_doc(step, spec, task, send)
    if step["type"] == "wait_for":
        return run_wait_for(step, spec, task, send)
    return _run(step, spec, task, send)


# ---------------- email ----------------
def email_preview(state: dict[str, Any]) -> str:
    d = state["draft"]
    to = state.get("to") or "❓ (couldn't find the support email — please type the address)"
    src = f"\n(source: {state['source']})" if state.get("source") else ""
    demo = "\n[Demo mode: the mail goes to your own inbox]" if settings().email_demo_override else ""
    return f"📧 Draft mail\nTo: {to}{src}\nSubject: {d['subject']}\n\n{d['body']}\n{demo}\nShall I send it? (yes / no / type the correct email)"


def run_email(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    state = task["steps"].get(step["id"], {})
    if state.get("status") == "confirmed":
        if not state.get("to"):
            send("I don't have an email address — please type it.")
            return {**state, "status": "awaiting_confirm"}
        used = email_action.send(state["to"], state["draft"]["subject"], state["draft"]["body"])
        store().add_event(task["task_id"], "email_sent", {"to": state["to"], "delivered_to": used})
        return {**state, "status": "done", "output": {"to": state["to"], "delivered_to": used,
                                                       "subject": state["draft"]["subject"],
                                                       "demo_override": settings().email_demo_override}}
    ctx = tctx(task)
    to = validate.email(render(step["to"], ctx))
    source = "you" if to else None
    if not to and step.get("to_lookup"):
        found = lookup.find_email(render(step["to_lookup"], ctx), task["task_id"])
        to, source = validate.email(found.email), found.source_url
    draft = decider.draft_email(render(step["goal"], ctx), task["inputs"], step.get("shareable", []), task["task_id"])
    new = {"status": "awaiting_confirm", "to": to, "source": source, "draft": draft.model_dump()}
    send(email_preview(new))
    return new


def _run(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    if step["type"] == "call":
        return run_call(step, spec, task)
    if step["type"] == "reply":
        return run_reply(step, spec, task, send)
    if step["type"] == "email":
        return run_email(step, spec, task, send)
    raise NotImplementedError(f"step type {step['type']} NOT BUILT yet")
