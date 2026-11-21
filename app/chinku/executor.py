"""Run a Plan (CLAUDE.md §5): steps in order, approval gate before anything leaves the device, results stored,
errors always told in plain words. Persistent: a restart resumes waiting tasks (state in the store)."""
import json
import re
import time
import uuid
from typing import Any, Callable

from app.engine import runner
from app.chinku import planner, tools
from app.chinku.models import Plan, StepState, Task
from app.store.dynamo import store

Send = Callable[[str], None]
Card = Callable[[str, dict[str, Any] | None], None]
REF = re.compile(r"\{(s\d+|last)(?:\.([\w.\[\]]+))?\}")
MISSING = re.compile(r"\{missing:([^}]*)\}")

STEP_WORDS = {"analyze": "look at the {what}", "transcribe_image": "type out the page exactly", "extract": "pull out the key details",
              "search": "search: {query}", "map": "show the places on a map", "call": "call {who}", "draft_email": "write the email to {to}",
              "send_email": "send it (I'll ask you first)", "remind": "remind you: {text}", "remember": "remember that",
              "recall": "check what I remember", "compute": "calculate", "reply": "answer",
              "recipe": "write the recipe", "shop_list": "find the ingredients on Swiggy Instamart", "cart_add": "add them to your Swiggy cart (I'll ask you first)",
              "compare": "compare options", "find_support": "find the official support contact",
              "promise": "track their promise", "promise_update": "update the promise", "scam_check": "check if it's a scam"}


# ---------------- persistence ----------------
def save(t: Task) -> None:
    t.updated_at = time.time()
    store().put("contacts", f"itask:{t.task_id}", {"kind": "itask", **json.loads(t.model_dump_json())})


def load(task_id: str) -> Task | None:
    d = store().get("contacts", f"itask:{task_id}")
    return Task.model_validate({k: v for k, v in d.items() if k not in ("kind", "updated_at")} | {"updated_at": d.get("updated_at", 0)}) if d else None


def _remember_output(user: str, name: str, value: Any) -> None:
    d = store().get("contacts", f"ilast:{user}") or {"kind": "ilast", "outputs": {}}
    d["outputs"][name] = value
    store().put("contacts", f"ilast:{user}", d)


# ---------------- references ----------------
def _dig(v: Any, path: str | None) -> Any:
    path = re.sub(r"\[(\d+)\]", r".\1", path or "").strip(".")  # phones[0] == phones.0
    for p in path.split(".") if path else []:
        v = v.get(p) if isinstance(v, dict) else (v[int(p)] if isinstance(v, list) and p.isdigit() and int(p) < len(v) else None)
    return v


def resolve(v: Any, t: Task) -> Any:
    if isinstance(v, dict):
        return {k: resolve(x, t) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, t) for x in v]
    if not isinstance(v, str) or "{" not in v:
        return v

    def get(sid: str, path: str | None) -> Any:
        if sid == "last":
            outs = (store().get("contacts", f"ilast:{t.user}") or {}).get("outputs") or {}
            name, _, rest = (path or "").partition(".")
            return _dig(outs.get(name), rest or None)
        st = t.steps.get(sid)
        return _dig(st.output if st else None, path)
    def val(m: re.Match) -> Any:
        x = get(m.group(1), m.group(2))
        return f"{{missing:{m.group(0)[1:-1]}}}" if x is None or x == "" else x  # never silently blank
    m = REF.fullmatch(v.strip())
    if m:
        return val(m)
    return REF.sub(lambda m: str(val(m)), v)


def missing_refs(inputs: Any) -> list[str]:
    return MISSING.findall(json.dumps(inputs, ensure_ascii=False, default=str))


# ---------------- readback ----------------
def step_line(i: int, s: Any) -> str:
    inp = s.inputs or {}
    who = ", ".join(str(x.get("name") or x.get("phone")) for x in inp.get("targets") or [] if isinstance(x, dict)) or "them"
    if "{" in who:  # filled in from an earlier step (e.g. the photo) — say so in words
        who = "the number on the photo"
    words = STEP_WORDS.get(s.tool, s.tool).format(what="photo", query=str(inp.get("query", ""))[:60], who=who,
                                                  to="you" if str(inp.get("to", "")).lower() in ("me", "myself") else inp.get("to", "?"),
                                                  text=str(inp.get("text", ""))[:60])
    if s.tool == "remind" and inp.get("when_iso"):
        try:
            words += f" — {tools.when_words(tools.parse_when(inp['when_iso']))}"
        except tools.ToolError:
            pass
    return f"{i}) {words}"


# ---------------- run ----------------
def _ctx(t: Task, send: Send, card: Card, step_id: str) -> dict[str, Any]:
    return {"task": t, "attachments": t.attachments, "card": card, "send": send, "step_id": step_id}


def _preview(tool: str, inputs: dict[str, Any]) -> dict[str, Any]:
    if tool == "send_email":
        d = inputs.get("draft") or inputs
        return {"type": "email", "state": "waiting", "to": d.get("to"), "subject": d.get("subject"), "body": d.get("body"),
                "attachment": (d.get("attachment") or {}).get("name")}
    if tool == "cart_add":
        return {"type": "cart_plan", "state": "waiting", "provider": "Swiggy Instamart", "status": "LIVE", "rows": inputs.get("rows") or [],
                "total": inputs.get("total"), "count": len(inputs.get("items") or []),
                "note": "Adds to your Swiggy cart only. You check out and pay in the Swiggy app — Chinku never pays."}
    if tool == "remind" and inputs.get("call_phone"):
        return {"type": "call_plan", "targets": [{"name": inputs.get("for_person") or "them", "phone": inputs.get("call_phone")}],
                "purpose": f"Reminder call at the set time: {inputs.get('text')}", "questions": [], "language": inputs.get("call_language")}
    if tool == "call":
        return {"type": "call_plan", "targets": inputs.get("targets"), "purpose": inputs.get("purpose"), "questions": inputs.get("questions")}
    return {"type": "approval", "tool": tool, "inputs": inputs}


def run(t: Task, send: Send, card: Card) -> Task:
    t.status = "running"
    for s in t.plan.steps:
        st = t.steps.setdefault(s.id, StepState())
        if st.status in ("done", "skipped", "failed"):
            continue
        if st.status == "waiting" and st.output.get("async"):
            save(t)
            return t  # a call is in progress; tick() resumes
        bad = [d for d in s.depends_on if t.steps.get(d) and t.steps[d].status in ("failed", "skipped")]
        if bad:
            st.status, st.error = "skipped", f"skipped because step {bad[0]} did not finish"
            continue
        inputs = resolve(s.inputs, t)
        gaps = missing_refs(inputs)
        if gaps:  # never ask approval for (or run) a step with a hole in it
            st.status, st.error = "failed", f"missing {', '.join(gaps)}"
            what = {"phones": "a phone number", "emails": "an email address", "text": "the text"}
            hint = next((w for k, w in what.items() if any(k in g for g in gaps)), "a detail from the earlier step")
            send(f"⚠️ I couldn't find {hint} for this step, so I didn't do it. Please type it in.")
            save(t)
            continue
        needs = s.needs_approval or s.tool in tools.ALWAYS_APPROVE or (s.tool == "remind" and bool(inputs.get("call_phone")))
        if needs and not st.output.get("approved"):
            st.status, st.preview = "waiting", _preview(s.tool, inputs)
            t.status = "waiting"
            save(t)
            store().put("contacts", f"iwait:{t.user}", {"kind": "iwait", "task_id": t.task_id, "step_id": s.id})
            what = {"send_email": f"Send this email to {st.preview.get('to')}?",
                    "cart_add": f"Add {st.preview.get('count')} items (₹{st.preview.get('total') or 0:,.0f}) to your Swiggy cart?",
                    "call": "Call " + ", ".join(f"{x.get('name') or ''} {x.get('phone') or ''}".strip() for x in inputs.get("targets") or []) + "?"}
            card(f"👉 Needs you: {what.get(s.tool, 'Go ahead?')} (yes / no)", st.preview)
            return t
        fn = tools.REGISTRY.get(s.tool)
        st.status = "running"
        try:
            if not fn:
                raise tools.ToolError(f"I can't do “{s.tool}” yet.")
            out = fn(t.user, inputs, _ctx(t, send, card, s.id))
            st.output = {**st.output, **out}
            if out.get("async"):
                st.status = "waiting"
                t.status = "waiting"
                save(t)
                return t
            st.status = "done"
            if s.output_name:
                _remember_output(t.user, s.output_name, out)
            _remember_output(t.user, s.tool, out)
        except tools.ToolError as e:
            st.status, st.error = "failed", str(e)
            send(f"⚠️ {e}")
        except Exception as e:  # noqa: BLE001
            st.status, st.error = "failed", str(e)[:300]
            store().add_event(t.task_id, "error", {"where": f"chinku:{s.tool}", "error": str(e)[:500]})
            send(f"⚠️ Sorry — the step “{STEP_WORDS.get(s.tool, s.tool).split(' {')[0]}” failed. The other steps carry on.")
        save(t)
    states = [x.status for x in t.steps.values()]
    t.status = "done" if all(x in ("done",) for x in states) else "partial" if "done" in states else "failed"
    save(t)
    if len(t.plan.steps) > 1:
        ok = sum(1 for x in states if x == "done")
        send(f"✅ Done — {ok} of {len(states)} steps finished." if t.status == "done"
             else f"Finished {ok} of {len(states)} steps. " + "; ".join(f"{k}: {v.error}" for k, v in t.steps.items() if v.error)[:400])
    return t


# ---------------- entry ----------------
def handle(user: str, text: str, attachments: list[dict[str, Any]], send: Send, card: Card) -> None:
    text = (text or "").strip()
    # 1) answer to a waiting approval
    w = store().get("contacts", f"iwait:{user}")
    if w and text and not attachments and (runner.YES.match(text) or runner.NO.match(text)):
        t = load(w["task_id"])
        store().delete("contacts", f"iwait:{user}")
        if t and t.steps.get(w["step_id"]) and t.steps[w["step_id"]].status == "waiting":
            st = t.steps[w["step_id"]]
            if runner.YES.match(text):
                st.status, st.output = "pending", {**st.output, "approved": True, "approved_at": time.time()}
                run(t, send, card)
            else:
                st.status, st.error = "skipped", "you said no"
                send("Okay — not sent. Nothing left the app.")
                run(t, send, card)
            return
    # 2) new request → (analyze a lone attachment) → plan → readback → run
    analysis = None
    t = Task(task_id="i_" + uuid.uuid4().hex[:8], user=user, request=text, attachments=attachments, plan=Plan(understood=""),
             created_at=time.time(), updated_at=time.time())
    if attachments and not text:
        try:
            analysis = tools.analyze(user, {"attachment": attachments[-1]["id"]}, _ctx(t, send, card, "a"))
        except Exception as e:  # noqa: BLE001
            store().add_event("-", "error", {"where": "chinku:analyze", "error": str(e)[:300]})
        if analysis:
            _remember_output(user, "analysis", analysis)
            sug = analysis.get("suggestions") or []
            card(f"📎 {analysis.get('title') or 'Got it'}. {analysis.get('summary') or ''}\nWhat should I do with this?",
                 {"type": "suggest", "title": analysis.get("title"), "kind": analysis.get("kind"), "image_url": analysis.get("image_url"),
                  "actions": [{"label": x["label"], "instruction": x["instruction"]} for x in sug[:3]]})
            store().put("contacts", f"iatt:{user}", {"kind": "iatt", "attachments": attachments, "ts": time.time()})
            return
    if not attachments and text:  # "transcribe it" right after sending a photo → reuse that photo
        recent = store().get("contacts", f"iatt:{user}")
        refers = re.search(r"\b(this|that|it|the|same)\s*(photo|picture|pic|image|page|screenshot|card|poster|bill|letter|pdf|one)?\b"
                           r"|\b(photo|picture|image|page|screenshot|transcri|attach)", text, re.I)
        photo_words = re.search(r"(photo|picture|pic|image|page|screenshot|transcri|attach|card|poster|bill|letter|pdf|"
                                r"type it|copy of (it|this|that)|email (it|this|that))", text, re.I)
        if recent and time.time() - recent.get("ts", 0) < 1800 and refers and photo_words:
            attachments = t.attachments = recent["attachments"]
    p = planner.plan(user, text, attachments, analysis)
    t.plan = p
    if p.clarifying_question or not p.steps or p.confidence < 0.8:
        save(t)
        send(p.clarifying_question or "Sorry, I didn't get that — could you say it again in one line?")
        return
    lines = [step_line(i, s) for i, s in enumerate(p.steps, start=1) if s.tool != "reply"]
    # a plain answer needs no readback; actions always get one (what Chinku understood + the steps)
    msg = (p.understood + ("\n" + "\n".join(lines) if len(lines) > 1 else "")) if lines else ""
    if p.cannot_do:
        msg += ("\n" if msg else "") + "\n".join(f"⛔ {c.what}: {c.why}" + (f" → {c.best_alternative}" if c.best_alternative else "") for c in p.cannot_do)
    if msg:
        send(msg)
    if attachments:
        store().put("contacts", f"iatt:{user}", {"kind": "iatt", "attachments": attachments, "ts": time.time()})
    run(t, send, card)


# ---------------- background: async calls + reminders ----------------
def tick(send_to: Callable[[str, str], None], card_to: Callable[[str, str, dict[str, Any] | None], None]) -> None:
    now = time.time()
    for r in tools.due_reminders(now):
        tools.fire_reminder(r, send_to)
    for p in tools.due_promises(now):
        tools.fire_promise(p, card_to)
    for d in store().scan("contacts", kind="itask"):
        if d.get("status") != "waiting":
            continue
        t = load(d["task_id"])
        for sid, st in (t.steps.items() if t else []):
            if st.status == "waiting" and st.output.get("async"):
                spec = store().get("tasks", st.output["spec_task_id"])
                if spec and spec.get("status") in ("done", "failed", "cancelled"):
                    calls = [call_summary(c) for c in store().scan("calls", task_id=spec["task_id"])]
                    st.output = {**st.output, "async": False, "calls": calls}
                    st.status = "done" if spec["status"] == "done" else "failed"
                    _remember_output(t.user, "call", st.output)
                    for c in calls:
                        card_to(t.user, call_headline(c), {"type": "call_result", **c})
                        auto_promises(t, c, card_to)
                    run(t, lambda x, u=t.user: send_to(u, x), lambda x, c, u=t.user: card_to(u, x, c))


BOOK_WORDS = re.compile(r"(confirm|book|appointment|slot|reserv|hold)", re.I)


def call_summary(c: dict[str, Any]) -> dict[str, Any]:
    """Answers with their status + the person's exact words. 'Booked' only when a booking field is CONFIRMED (code rule)."""
    fields = (c.get("extracted") or {}).get("fields") or []
    answers = [{"name": f.get("name"), "value": f.get("value"), "status": f.get("status"), "quote": f.get("evidence_quote")} for f in fields]
    booked = any(BOOK_WORDS.search(a["name"] or "") and a["status"] == "confirmed" and str(a["value"]).strip().lower()
                 not in ("", "none", "no", "false", "not available") for a in answers)
    return {"who": (c.get("callee") or {}).get("name") or (c.get("callee") or {}).get("role"), "to_last4": (c.get("to_number") or "")[-4:],
            "status": c.get("status"), "outcome": c.get("outcome"), "answers": answers, "booked": booked,
            "transcript": (c.get("transcript") or "")[:3000], "call_id": c.get("call_id")}


def call_headline(c: dict[str, Any]) -> str:
    if c["status"] == "no_answer":
        return f"📞 {c['who']} didn't answer."
    if c["status"] != "completed":
        return f"📞 Call to {c['who']}: {c['status']}."
    sure = [a for a in c["answers"] if a["status"] == "confirmed"]
    return f"📞 Call to {c['who']} done" + (" · Booked ✅" if c["booked"] else "") + f" · {len(sure)} of {len(c['answers'])} answers confirmed."


class _Prom(tools.BaseModel):
    who: str
    what: str
    by_when_iso: str | None = None
    ticket: str | None = None
    evidence_quote: str


class _Proms(tools.BaseModel):
    promises: list[_Prom] = []


def auto_promises(t: Task, c: dict[str, Any], card_to: Callable[[str, str, dict[str, Any] | None], None]) -> None:
    """Any commitment heard on the call becomes a tracked Promise (only with an exact quote that is really in the transcript)."""
    tr = c.get("transcript") or ""
    if c.get("status") != "completed" or not re.search(r"(call ?back|will|by|tomorrow|today|within|days|hours|ticket|complaint|refund|technician|visit)", tr, re.I):
        return
    from datetime import datetime
    try:
        out = tools.json_call(tools.settings().openai_model_brain,
                              "From this phone-call transcript, list promises THE OTHER PERSON made (callback, technician visit, refund, "
                              "delivery, fix). by_when_iso: ISO 8601 with +05:30 resolved from now_ist, null if no time. ticket: complaint/"
                              "ticket/reference number exactly as said. evidence_quote: their exact words. No promise → empty list. JSON only.",
                              json.dumps({"now_ist": datetime.now(tools.IST).isoformat(timespec="minutes"), "transcript": tr}, ensure_ascii=False),
                              _Proms, t.task_id, "promises")
    except Exception:  # noqa: BLE001
        return
    for p in out.promises:
        if not p.by_when_iso or not p.evidence_quote or p.evidence_quote.strip()[:30].lower() not in tr.lower():
            continue  # no time or no real quote → not tracked as a promise
        try:
            tools.promise(t.user, {"who": c.get("who") or p.who, "what": p.what, "by_when_iso": p.by_when_iso, "ticket": p.ticket,
                                   "evidence_quote": p.evidence_quote},
                          {"task": t, "card": lambda x, cd, u=t.user: card_to(u, x, cd)})
        except tools.ToolError:
            continue
