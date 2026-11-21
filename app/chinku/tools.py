"""Chinku tools (CLAUDE.md §4). Each tool: fn(user, inputs, ctx) -> output dict (JSON-safe).
ctx = {"task": Task, "attachments": [...], "card": fn(text, card)}. The LLM understands; CODE owns times, numbers,
recipients and anything that leaves the device (approval gate lives in executor.py)."""
import ast
import json
import operator
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from app.brain.client import image_part, json_call, prompt
from app.config import settings
from app.chinku import memory
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
EMAIL_RX = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


class ToolError(Exception):
    """A plain-words reason shown to the user (never silent)."""


# ---------------- helpers ----------------
def when_words(dt: datetime) -> str:
    now = datetime.now(IST)
    d = (dt.date() - now.date()).days
    day = "Today" if d == 0 else "Tomorrow" if d == 1 else dt.strftime("%A")
    return f"{day}, {dt.strftime('%a %d %b')}, {dt.strftime('%I:%M %p').lstrip('0')}"


def parse_when(iso: str | None) -> datetime:
    if not iso:
        raise ToolError("I need a time for the reminder.")
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"I couldn't understand the time “{iso}”.") from e
    dt = dt if dt.tzinfo else dt.replace(tzinfo=IST)
    dt = dt.astimezone(IST)
    if dt <= datetime.now(IST) - timedelta(minutes=1):
        raise ToolError(f"{when_words(dt)} is already past — which time should I use?")
    return dt


def attachment(ctx: dict[str, Any], ref: str | None) -> dict[str, Any]:
    atts = ctx.get("attachments") or []
    if not atts:
        raise ToolError("I don't see a photo or file — please attach it.")
    a = next((a for a in atts if a["id"] == ref), None) if ref else None
    return a or atts[-1]


def att_bytes(a: dict[str, Any]) -> bytes:
    return Path(a["path"]).read_bytes()


# ---------------- analyze / transcribe / extract ----------------
class KeyFact(BaseModel):
    label: str
    value: str
    quote: str | None = None


class Sug(BaseModel):
    label: str
    instruction: str


class Analysis(BaseModel):
    kind: str = "other"
    title: str = ""
    summary: str = ""
    key_facts: list[KeyFact] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    brand: str | None = None
    model: str | None = None
    problem: str | None = None
    suggestions: list[Sug] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list)


def analyze(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    a = attachment(ctx, inputs.get("attachment"))
    if a["mime"].endswith("pdf"):
        from app.brain.perceive import read_document
        d = read_document(att_bytes(a), a["name"])
        out = Analysis(kind="printed_document", title=d.title or d.doc_type, summary=d.summary or "",
                       key_facts=[KeyFact(label=li.label, value=str(li.amount)) for li in d.line_items[:12]])
    else:
        out = json_call(settings().openai_model_brain, prompt("analyze_image"),
                        [{"type": "text", "text": "What is this? " + (inputs.get("focus") or "")}, image_part(att_bytes(a), a["mime"])],
                        Analysis, ctx["task"].task_id, "analyze")
    return out.model_dump() | {"attachment": a["id"], "image_url": a.get("url")}


class Transcript(BaseModel):
    text: str = ""
    kind: str = "none"
    language: str = ""
    unclear_count: int = 0
    title: str = ""


def transcribe_image(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    a = attachment(ctx, inputs.get("attachment"))
    if a["mime"].endswith("pdf"):
        raise ToolError("Exact transcription works on photos; for a PDF I can read and summarise it instead.")
    t = json_call(settings().openai_model_brain, prompt("transcribe_image"),
                  [{"type": "text", "text": "Transcribe exactly."}, image_part(att_bytes(a), a["mime"])],
                  Transcript, ctx["task"].task_id, "transcribe_image")
    t.unclear_count = t.text.count("[unclear]")  # code counts, not the model
    if not t.text.strip():
        raise ToolError("I couldn't find any text in that photo.")
    ctx["card"](f"📝 Transcript ready{' · ' + str(t.unclear_count) + ' unclear word(s) marked [unclear]' if t.unclear_count else ''}.",
                {"type": "transcript", "title": t.title, "text": t.text, "unclear": t.unclear_count, "image_url": a.get("url")})
    return t.model_dump() | {"attachment": a["id"], "image_url": a.get("url")}


class Facts(BaseModel):
    facts: list[KeyFact] = Field(default_factory=list)


def extract(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    text = str(inputs.get("text") or "")
    if not text:
        raise ToolError("There is no text to pull the details from.")
    sys = ("Extract the key facts the user asked for (dates, amounts, names, ids, phone numbers, deadlines). Each fact: "
           "{label, value exactly as written, quote = exact words from the text}. No quote → leave the fact out. Return JSON {\"facts\": [...]}.")
    f = json_call(settings().openai_model_brain, sys, json.dumps({"want": inputs.get("want"), "text": text[:12000]}, ensure_ascii=False),
                  Facts, ctx["task"].task_id, "extract")
    f.facts = [x for x in f.facts if x.quote and x.quote.strip() and x.quote.strip()[:40] in text]  # evidence must exist
    return f.model_dump()


# ---------------- search / map ----------------
class Place(BaseModel):
    name: str
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    url: str | None = None
    note: str | None = None


class Research(BaseModel):
    answer: str = ""
    items: list[Place] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    unsure: bool = False


def search(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.brain.client import oai
    q = str(inputs.get("query") or "")
    if not q:
        raise ToolError("What should I search for?")
    official = bool(inputs.get("official_only"))
    r = oai().responses.create(
        model=settings().openai_model_brain, tools=[{"type": "web_search"}],
        input=(f"Today is {datetime.now(IST):%d %b %Y} (India). Research: {q}\n"
               + ("Use ONLY the company's official website / official domain for contact details. " if official else "")
               + "Return ONLY JSON {\"answer\": short answer, \"items\": [{\"name\", \"address\", \"phone\", \"email\", \"url\", \"note\"}], "
                 "\"sources\": [urls you used], \"unsure\": true|false}. Max 6 items. Never invent a phone, email or address — null if not found."))
    txt = r.output_text or ""
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        res = Research.model_validate(json.loads(m.group(0))) if m else Research(answer=txt[:800], unsure=True)
    except Exception:  # noqa: BLE001
        res = Research(answer=txt[:800], unsure=True)
    checked = datetime.now(IST).strftime("%d %b %Y")
    ctx["card"](f"🔎 {res.answer[:300]}" + (" (not fully sure)" if res.unsure else ""),
                {"type": "research", "query": q, "answer": res.answer, "items": [i.model_dump() for i in res.items],
                 "sources": res.sources[:6], "checked": checked, "unsure": res.unsure})
    return res.model_dump() | {"checked": checked}


def map_(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.actions import maps
    items = inputs.get("items") or []
    rows = []
    for i, it in enumerate(items[:6], start=1):
        it = it if isinstance(it, dict) else {"name": str(it)}
        g = None
        try:
            if it.get("address"):
                g = maps.geocode(it["address"]) or maps.geocode(f"{it['name']}, {it['address']}")
        except Exception:  # noqa: BLE001
            g = None
        rows.append({"n": i, "name": it.get("name"), "address": it.get("address"), "phone": it.get("phone"),
                     "lat": g[0] if g else None, "lon": g[1] if g else None, "source": it.get("url"),
                     "map_url": f"https://www.openstreetmap.org/?mlat={g[0]}&mlon={g[1]}#map=17/{g[0]}/{g[1]}" if g else None})
    if not rows:
        raise ToolError("I didn't find any places to show on the map.")
    ctx["card"]("🗺️ " + (inputs.get("title") or "Places I found") + " — which one?", {"type": "map", "rows": rows})
    return {"rows": rows}


# ---------------- email ----------------
class Draft(BaseModel):
    subject: str
    body: str


def resolve_email(user: str, to: str | None) -> str:
    t = (to or "").strip()
    if not t:
        raise ToolError("Who should I email? Give me the email address.")
    if t.lower() in ("me", "myself", "my email", "self", "user"):
        mine = memory.get(user, "my email") or settings().smtp_user
        if not mine:
            raise ToolError("What's your email address? (Say “my email is …” and I'll remember it.)")
        return mine
    if EMAIL_RX.match(t):
        return t
    for key in (f"{t} email", f"{t}'s email", t):
        v = memory.get(user, key)
        if isinstance(v, str) and EMAIL_RX.match(v.strip()):
            return v.strip()
    raise ToolError(f"I don't have {t}'s email address. What is it? (I'll remember it.)")


def draft_email(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    to = resolve_email(user, inputs.get("to"))
    content = inputs.get("content")
    content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1) if content else ""
    mode = inputs.get("mode") or "write"
    if mode == "exact_copy":  # transcripts go EXACTLY as transcribed — never rewritten
        subj = inputs.get("subject") or "Transcript" + (f": {inputs.get('title')}" if inputs.get("title") else "")
        body = (inputs.get("intro") or "Here is the exact transcript of the page:") + "\n\n" + content + "\n\n— Sent by Chinku for " + settings().user_name
        d = Draft(subject=subj, body=body)
    else:
        sys = prompt("email_writer").replace("{user_name}", settings().user_name)
        d = json_call(settings().openai_model_brain, sys, json.dumps({"to": to, "purpose": inputs.get("purpose"), "language": inputs.get("language") or "English",
                                                                   "content": content[:8000]}, ensure_ascii=False), Draft, ctx["task"].task_id, "draft_email")
    att = None
    if inputs.get("attach") and ctx.get("attachments"):
        a = attachment(ctx, inputs.get("attach") if isinstance(inputs.get("attach"), str) and inputs.get("attach").startswith("a") else None)
        att = {"id": a["id"], "name": a["name"], "mime": a["mime"]}
    return {"to": to, "subject": d.subject, "body": d.body, "attachment": att}


def send_email(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.actions import email as email_action
    d = inputs.get("draft") or inputs
    to, subject, body = d.get("to"), d.get("subject"), d.get("body")
    if not (to and subject and body) or not EMAIL_RX.match(to):
        raise ToolError("The email draft is incomplete — I didn't send anything.")
    key = f"isend:{ctx['task'].task_id}:{ctx['step_id']}:{to}"
    if not store().claim(key):  # idempotent: a retry never sends twice
        return {"sent": True, "to": to, "duplicate": True}
    img, fname = None, None
    if d.get("attachment"):
        a = next((x for x in ctx.get("attachments") or [] if x["id"] == d["attachment"]["id"]), None)
        if a:
            img, fname = (att_bytes(a), a["mime"]), a["name"]
    used = email_action.send(to, subject, body, img, fname)
    ctx["card"](f"✉️ Sent to {to}" + (f" (demo: delivered to {used})" if used != to else ""),
                {"type": "email", "state": "sent", "to": to, "subject": subject, "body": body})
    return {"sent": True, "to": to, "delivered_to": used}


# ---------------- reminders ----------------
def remind(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    text = str(inputs.get("text") or "").strip()
    if not text:
        raise ToolError("What should I remind you about?")
    dt = parse_when(inputs.get("when_iso"))
    repeat = inputs.get("repeat") or "none"
    if repeat not in ("none", "daily", "weekly"):
        repeat = "none"
    rid = "r_" + uuid.uuid4().hex[:8]
    store().put("contacts", f"rem:{rid}", {"kind": "reminder", "owner": user, "rid": rid, "text": text, "due": dt.timestamp(),
                                          "repeat": repeat, "status": "scheduled", "task_id": ctx["task"].task_id,
                                          "for_person": inputs.get("for_person")})
    words = when_words(dt)
    ctx["card"](f"⏰ Reminder set · {words}{' · repeats ' + repeat if repeat != 'none' else ''}: {text}",
                {"type": "reminder", "rid": rid, "text": text, "when": words, "repeat": repeat})
    return {"rid": rid, "when": words, "due": dt.isoformat(), "text": text}


def due_reminders(now_ts: float) -> list[dict[str, Any]]:
    return [r for r in store().scan("contacts", kind="reminder") if r.get("status") == "scheduled" and r.get("due", 0) <= now_ts]


def fire_reminder(r: dict[str, Any], deliver: Callable[[str, str], None]) -> None:
    if not store().claim(f"remfire:{r['rid']}:{int(r['due'])}"):
        return
    deliver(r["owner"], f"⏰ Reminder: {r['text']}")
    if r.get("repeat") in ("daily", "weekly"):
        r["due"] += 86400 * (1 if r["repeat"] == "daily" else 7)
    else:
        r["status"] = "done"
    store().put("contacts", f"rem:{r['rid']}", r)


# ---------------- memory ----------------
def remember(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    items = inputs.get("items") or ([{"key": inputs.get("key"), "value": inputs.get("value"), "kind": inputs.get("kind")}] if inputs.get("key") else [])
    saved, refused = [], []
    for it in items:
        r = memory.remember(user, str(it.get("key") or ""), it.get("value"), it.get("kind") or "fact")
        (saved if r["saved"] else refused).append({"key": it.get("key"), **r})
    if refused and not saved:
        raise ToolError(refused[0]["reason"])
    ctx["card"]("🧠 Remembered: " + "; ".join(f"{s['key']} = {s['value']}" for s in saved)
                + ("" if not refused else " · Not saved: " + refused[0]["reason"]), {"type": "memory", "saved": saved})
    return {"saved": saved, "refused": refused}


def recall(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rows = memory.recall(user)
    q = str(inputs.get("about") or "").lower()
    if q:
        rows = [r for r in rows if q in r["key"].lower() or q in str(r["value"]).lower()] or rows
    return {"memory": rows}


# ---------------- compute / reply ----------------
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.USub: operator.neg}


def _eval(n: ast.AST) -> float:
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval(n.left), _eval(n.right))
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval(n.operand))
    raise ToolError("I can only do + − × ÷ on numbers.")


def compute(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    expr = str(inputs.get("expression") or "").replace(",", "").replace("×", "*").replace("÷", "/").replace("₹", "")
    v = _eval(ast.parse(expr, mode="eval").body)
    return {"expression": inputs.get("expression"), "value": round(v, 2)}


def reply(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    ctx["card"](str(inputs.get("text") or ""), None)
    return {"text": inputs.get("text")}


# ---------------- call (reuses the proven call engine) ----------------
def call(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.engine import runner
    from app.engine.validate import indian_mobile
    targets = []
    for t in inputs.get("targets") or []:
        ph = indian_mobile(str(t.get("phone") or ""))
        if not ph:
            raise ToolError(f"“{t.get('phone')}” is not a valid Indian mobile number.")
        targets.append({"label": t.get("name") or "the person", "phone": ph, "role": t.get("role") or "contact"})
    if not targets:
        raise ToolError("Whom should I call? I need a phone number.")
    qs = [{"field": re.sub(r"[^a-z0-9]+", "_", (q if isinstance(q, str) else q.get("question", "")).lower())[:40] or f"q{i}",
           "question": q if isinstance(q, str) else q.get("question")} for i, q in enumerate(inputs.get("questions") or [])]
    spec_task = runner.start_spec(user, "call_and_ask", {"targets": targets, "purpose": inputs.get("purpose") or "",
                                                          "questions": qs or [{"field": "answer", "question": inputs.get("purpose")}]},
                                  ctx["send"])
    if spec_task.get("status") == "failed":
        raise ToolError("I couldn't start the call: " + ", ".join(spec_task.get("problems") or ["unknown problem"]))
    return {"spec_task_id": spec_task["task_id"], "async": True, "targets": targets}


REGISTRY: dict[str, Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "analyze": analyze, "transcribe_image": transcribe_image, "extract": extract, "search": search, "map": map_,
    "draft_email": draft_email, "send_email": send_email, "remind": remind, "remember": remember, "recall": recall,
    "compute": compute, "reply": reply, "call": call,
}
ALWAYS_APPROVE = {"send_email", "call"}  # anything that leaves the device — enforced in code, whatever the plan says
