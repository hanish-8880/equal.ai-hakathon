"""Chinku tools (CLAUDE.md §4). Each tool: fn(user, inputs, ctx) -> output dict (JSON-safe).
ctx = {"task": Task, "attachments": [...], "card": fn(text, card)}. The LLM understands; CODE owns times, numbers,
recipients and anything that leaves the device (approval gate lives in executor.py)."""
import ast
import json
import operator
import re
import time
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
    if not inputs.get("silent"):
        ctx["card"]("📋 " + ("\n".join(f"{x.label}: {x.value}" for x in f.facts) if f.facts else "I couldn't find those details in the text."), None)
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
    r = oai().with_options(timeout=180).responses.create(
        model=settings().openai_model_brain, tools=[{"type": "web_search"}], reasoning={"effort": "low"},
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
        g, how = None, None
        addr = str(it.get("address") or "")
        parts = [p.strip() for p in addr.split(",") if p.strip() and not re.search(r"\d+(st|nd|rd|th)? floor|^sy|^plot|^#|^no\.?\s*\d", p.strip(), re.I)]
        tries = [(addr, "address"), (f"{it.get('name')}, Hyderabad", "name"),
                 (", ".join(parts[-3:]), "area"), (", ".join(parts[-2:]), "area")]  # long Indian addresses → locality fallback
        for q, kind in tries:
            if not q or not q.strip(", "):
                continue
            try:
                g = maps.geocode(q)
            except Exception:  # noqa: BLE001
                g = None
            time.sleep(1.0)  # Nominatim usage policy: max 1 request per second
            if g:
                how = kind
                break
        rows.append({"n": i, "name": it.get("name"), "address": it.get("address"), "phone": it.get("phone"),
                     "lat": g[0] if g else None, "lon": g[1] if g else None, "source": it.get("url"), "pin": how,
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
    from app.engine.validate import indian_mobile
    call_phone = indian_mobile(str(inputs.get("call_phone") or "")) if inputs.get("call_phone") else None
    if inputs.get("call_phone") and not call_phone:
        raise ToolError(f"“{inputs.get('call_phone')}” is not a valid Indian mobile number for the reminder call.")
    store().put("contacts", f"rem:{rid}", {"kind": "reminder", "owner": user, "rid": rid, "text": text, "due": dt.timestamp(),
                                          "repeat": repeat, "status": "scheduled", "task_id": ctx["task"].task_id,
                                          "for_person": inputs.get("for_person"), "call_phone": call_phone,
                                          "call_language": inputs.get("call_language") or "English"})
    words = when_words(dt)
    who = f" · I'll call {inputs.get('for_person') or 'them'}" if call_phone else ""
    ctx["card"](f"⏰ Reminder set · {words}{' · repeats ' + repeat if repeat != 'none' else ''}{who}: {text}",
                {"type": "reminder", "rid": rid, "text": text, "when": words, "repeat": repeat,
                 "call": bool(call_phone), "for_person": inputs.get("for_person")})
    return {"rid": rid, "when": words, "due": dt.isoformat(), "text": text}


def due_reminders(now_ts: float) -> list[dict[str, Any]]:
    return [r for r in store().scan("contacts", kind="reminder") if r.get("status") == "scheduled" and r.get("due", 0) <= now_ts]


def fire_reminder(r: dict[str, Any], deliver: Callable[[str, str], None]) -> None:
    if not store().claim(f"remfire:{r['rid']}:{int(r['due'])}"):
        return
    deliver(r["owner"], f"⏰ Reminder: {r['text']}")
    if r.get("call_phone"):  # reminder CALL to someone (approved when the reminder was set)
        from app.engine import runner
        try:
            runner.start_spec(r["owner"], "call_and_ask", {
                "targets": [{"label": r.get("for_person") or "them", "phone": r["call_phone"], "role": "family"}],
                "purpose": f"Remind {r.get('for_person') or 'them'}: {r['text']}. Say it slowly and clearly, then ask if they understood.",
                "questions": [{"field": "understood", "question": "Did they understand the reminder?"}],
                "language": r.get("call_language") or "English"}, lambda t, u=r["owner"]: deliver(u, t))
            deliver(r["owner"], f"📞 Calling {r.get('for_person') or 'them'} with the reminder.")
        except Exception as e:  # noqa: BLE001 — never silent
            deliver(r["owner"], f"⚠️ I couldn't place the reminder call to {r.get('for_person') or 'them'}: {str(e)[:120]}")
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
    if not inputs.get("silent"):
        ctx["card"]("🧠 " + ("; ".join(f"{r['key']}: {r['value']}" for r in rows[:12]) if rows else "I don't remember anything about that yet."),
                    {"type": "memory", "saved": [{"key": r["key"], "value": r["value"]} for r in rows[:12]]} if rows else None)
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
    try:
        v = _eval(ast.parse(expr, mode="eval").body)
    except (SyntaxError, ZeroDivisionError) as e:
        raise ToolError("I couldn't calculate that — please write it like “714 × 50”.") from e
    v = round(v, 2)
    shown = f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    if not inputs.get("silent"):
        ctx["card"](f"🧮 {inputs.get('expression')} = {shown}", None)  # code computes; the user always sees the answer
    return {"expression": inputs.get("expression"), "value": v, "shown": shown}


def reply(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    ctx["card"](str(inputs.get("text") or ""), None)
    return {"text": inputs.get("text")}


def quiet_hours(now: datetime | None = None) -> str | None:
    """21:00–08:00 IST: no calls to numbers the user hasn't saved (CLAUDE.md §10: call only people who agreed)."""
    now = now or datetime.now(IST)
    return now.strftime("%I:%M %p").lstrip("0") if (now.hour >= 21 or now.hour < 8) else None


def known_numbers(user: str) -> set[str]:
    """Numbers the user saved in memory (family, friends, volunteers who agreed) + the user's own phone."""
    from app.engine.validate import indian_mobile
    out = {indian_mobile(settings().user_whatsapp.replace("whatsapp:", ""))}
    for r in memory.recall(user):
        for m in re.findall(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}", str(r["value"])):
            out.add(indian_mobile(m))
    return {x for x in out if x}


def check_call_hours(user: str, targets: list[dict[str, Any]]) -> None:
    from app.engine.validate import indian_mobile
    late = quiet_hours()
    if not late:
        return
    known = known_numbers(user)
    from app.engine.validate import phone_kind
    strangers = [t for t in targets if phone_kind(t.get("phone"))[1] != "tollfree" and phone_kind(t.get("phone"))[0] not in known]
    if strangers:
        names = ", ".join(str(t.get("label") or t.get("name") or t.get("phone")) for t in strangers)
        raise ToolError(f"It's {late} — too late to call {names} (businesses and people who haven't agreed to calls). "
                        f"Say “call them at 10 AM” and I'll set a reminder, or save their number first if they agreed.")


# ---------------- call (reuses the proven call engine) ----------------
def call(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.engine import runner
    from app.engine.validate import indian_mobile
    targets = []
    for t in inputs.get("targets") or []:
        from app.engine.validate import phone_kind
        ph, kind = phone_kind(t.get("phone"))
        if not ph:
            raise ToolError(f"“{t.get('phone')}” is not a number I can call (mobile, landline or 1800 toll-free).")
        targets.append({"label": t.get("name") or "the person", "phone": ph, "role": t.get("role") or "contact", "kind": kind})
    if not targets:
        raise ToolError("Whom should I call? I need a phone number.")
    check_call_hours(user, targets)
    qs = [{"field": re.sub(r"[^a-z0-9]+", "_", (q if isinstance(q, str) else q.get("question", "")).lower())[:40] or f"q{i}",
           "question": q if isinstance(q, str) else q.get("question")} for i, q in enumerate(inputs.get("questions") or [])]
    lang = inputs.get("language") or "English"
    spec_task = runner.start_spec(user, "call_and_ask", {"targets": targets, "purpose": inputs.get("purpose") or "",
                                                          "questions": qs or [{"field": "answer", "question": inputs.get("purpose")}],
                                                          "language": lang, "chinku_task": ctx["task"].task_id},
                                  ctx["send"])
    ctx["card"](f"📞 Calling {', '.join(t['label'] for t in targets)}" + (f" in {lang}" if lang != "English" else "") + "…",
                {"type": "call_live", "targets": [{"name": t["label"], "phone": t["phone"][-4:]} for t in targets], "language": lang,
                 "spec_task_id": spec_task.get("task_id"), "purpose": inputs.get("purpose")})
    if spec_task.get("status") == "failed":
        raise ToolError("I couldn't start the call: " + ", ".join(spec_task.get("problems") or ["unknown problem"]))
    return {"spec_task_id": spec_task["task_id"], "async": True, "targets": targets}


# ---------------- compare across connected services (CLAUDE.md §6A, J9) ----------------
def compare(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Ask every connected provider of a category, rank in CODE by this request's priority, explain the winner.
    Chinku never pays: the card's button opens the provider's own booking page."""
    from app.chinku import adapters
    cat = str(inputs.get("category") or "").lower()
    provs = adapters.registry().get(cat)
    if not provs:
        raise ToolError(f"I can't compare “{cat}” yet — connected today: {', '.join(adapters.registry())}.")
    priority = inputs.get("priority") if inputs.get("priority") in ("cheapest", "fastest", "earliest") else "cheapest"
    req = dict(inputs) | {"priority": priority}
    if cat == "ride":
        if not (req.get("from") and req.get("to")):
            raise ToolError("Where from and where to? (Say “my home is …” once and I'll remember it.)")
        from app.chinku.adapters import rides
        try:  # geocode once, share with every provider
            req["_from_ll"], req["_to_ll"] = rides.where(str(req["from"])), rides.where(str(req["to"]))
        except ValueError as e:
            raise ToolError(str(e)) from e
        if inputs.get("arrive_by"):
            req["arrive_by"] = inputs["arrive_by"]
    if cat == "flight":
        if not (req.get("from") and req.get("to") and req.get("date")):
            raise ToolError("For flights I need from, to and the date.")
        try:
            day = datetime.fromisoformat(str(req["date"])[:10]).date()
        except ValueError as e:
            raise ToolError(f"I couldn't read the date “{req['date']}”.") from e
        if day < datetime.now(IST).date():
            raise ToolError(f"{day:%a %d %b} is already past — which date?")
    offers, failed = [], []
    for p in provs:
        try:
            offers += p.search(req)
        except Exception as e:  # noqa: BLE001 — one provider failing never hides the others
            failed.append(f"{p.name}: {str(e)[:120]}")
    rec = adapters.rank(offers, priority, inputs.get("max_stops"), inputs.get("arrive_by"))
    if not rec:
        raise ToolError("No options found" + (f" ({'; '.join(failed)})" if failed else "") + ".")
    w = rec.winner
    head = (f"{req.get('from')} → {req.get('to')} · {day:%a %d %b} · {int(req.get('adults') or 1)} adult(s)" if cat == "flight"
            else f"{req.get('from')} → {req.get('to')}" + (f" · arrive by {datetime.fromisoformat(inputs['arrive_by']).strftime('%I:%M %p').lstrip('0')}"
                                                         if inputs.get("arrive_by") else "") if cat == "ride" else cat)
    card = {"type": "compare", "category": cat, "title": head, "priority": priority,
            "winner": w.model_dump(), "why": rec.why, "change": rec.what_would_change,
            "rows": [o.model_dump() for o in rec.table[:8]], "excluded": rec.excluded, "providers": [p.name for p in provs],
            "status": sorted({p.status for p in provs}), "failed": failed, "checked": w.fetched_at, "total_found": len(offers)}
    ctx["card"](f"{'✈️' if cat == 'flight' else '🛺'} Recommended ({priority}): {w.item} — "
                f"{'₹' + format(w.final_payable, ',.0f') if w.currency == 'INR' and w.final_payable is not None else w.final_payable}. "
                f"Why: {'; '.join(rec.why)}. Booking happens on {w.provider} — Chinku never pays.", card)
    return {"recommended": w.model_dump(), "why": rec.why, "count": len(offers), "priority": priority, "failed": failed}


# ---------------- recipe → ingredients → Swiggy Instamart cart (never checkout) ----------------
class Ingredient(BaseModel):
    name: str
    quantity: str = ""                 # as a cook says it: "500 g", "2 cups", "1 tsp"
    grams: float | None = None         # rough weight/volume to buy (g or ml) — null if unsure
    search_query: str                  # simple English shop query: "basmati rice", "chicken curry cut", "biryani masala"
    pantry: bool = False               # salt, water, sugar — most homes have it; not bought unless asked


class Recipe(BaseModel):
    dish: str
    servings: int = 4
    time_minutes: int | None = None
    ingredients: list[Ingredient] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    tip: str | None = None


PANTRY = {"salt", "water", "sugar"}


def recipe(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app import prefs
    from app.chinku.planner import LANG
    lang = LANG.get(prefs.get(user).get("language") or "en", "English")
    sys = (f"Write a home-style Indian recipe. Language for dish name, quantities and steps: {lang}. "
           "ingredients[].search_query ALWAYS in simple English as a shopper would search a grocery app (e.g. 'basmati rice', "
           "'chicken curry cut', 'biryani masala', 'curd', 'ghee', 'mint leaves', 'fried onion'). grams = amount to buy in g or ml "
           "(null if not a weight, e.g. 2 bay leaves). pantry=true only for salt, water, sugar. Max 14 ingredients, 6-9 short steps. "
           "Return JSON matching the schema.")
    r = json_call(settings().openai_model_brain, sys, json.dumps({"dish": inputs.get("dish"), "servings": inputs.get("servings") or 4,
                                                                 "notes": inputs.get("notes")}, ensure_ascii=False), Recipe, ctx["task"].task_id, "recipe")
    for i in r.ingredients:
        i.pantry = i.pantry or i.search_query.strip().lower() in PANTRY
    from app.chinku.adapters import swiggy
    ctx["card"](f"🍳 {r.dish} · {r.servings} people" + (f" · ~{r.time_minutes} min" if r.time_minutes else ""),
                {"type": "recipe", **r.model_dump(), "can_shop": swiggy.connected()})
    return r.model_dump()


def _pick(variants: list[dict[str, Any]], query: str, need_g: float | None) -> tuple[dict[str, Any] | None, int, str]:
    """Code chooses the pack and how many: relevant name → one pack that covers the amount (cheapest) →
    else the best value pack × as many as needed. Never the model."""
    import math
    from app.chinku.adapters.swiggy import grams
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2]
    rel = [v for v in variants if v["rank"] <= 3 and any(w in (v["name"] or "").lower() for w in words)]
    pool, note = (rel, "") if rel else ([v for v in variants if v["rank"] == 0], "check: closest match")
    if not pool:
        return None, 0, "not found"
    if need_g:
        enough = [v for v in pool if (grams(v["pack"]) or 0) >= need_g]
        if enough:
            return min(enough, key=lambda v: (v["price"] is None, v["price"] or 0)), 1, note
        sized = [v for v in pool if grams(v["pack"]) and v["price"]]
        if sized:  # no single pack is big enough → best value per 100 g, as many packs as needed (max 5)
            best = min(sized, key=lambda v: v["price"] / grams(v["pack"]))
            return best, min(5, math.ceil(need_g / grams(best["pack"]))), note
    return min(pool, key=lambda v: (v["price"] is None, v["price"] or 0)), 1, note


def _search_with_fallback(swiggy: Any, q: str) -> tuple[list[dict[str, Any]], str]:
    """'mint leaves' finds nothing but 'mint' does — retry once with the main word."""
    found = swiggy.search(q)
    if found:
        return found, q
    words = [w for w in re.findall(r"[A-Za-z]+", q) if w.lower() not in ("leaves", "leaf", "fresh", "whole", "powder", "paste")]
    if words and words[0].lower() != q.lower():
        found = swiggy.search(words[0])
        if re.search(r"\b(leaves|leaf|fresh)\b", q, re.I):  # asked for the herb, not seeds/powder
            found = [v for v in found if not re.search(r"\b(whole|seeds?|powder|dhania whole)\b", v.get("name") or "", re.I)]
        return [v | {"fallback": True} for v in found], words[0]
    return [], q


def shop_list(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.chinku.adapters import swiggy
    if not swiggy.connected():
        raise ToolError("Swiggy isn't connected yet — log in once with your phone + OTP.")
    ings = inputs.get("ingredients") or []
    if not isinstance(ings, list) or not ings:
        raise ToolError("Which ingredients should I look for? Ask me for a recipe first.")
    extra = {str(x).lower() for x in inputs.get("include") or []}
    rows, missing, skipped = [], [], []
    for ing in ings[:14]:
        ing = ing if isinstance(ing, dict) else {"name": str(ing), "search_query": str(ing)}
        q = ing.get("search_query") or ing.get("name")
        if ing.get("pantry") and q.lower() not in extra and ing.get("name", "").lower() not in extra:
            skipped.append(ing.get("name") or q)
            continue
        try:
            time.sleep(0.4)  # be gentle with Swiggy: many searches in a row
            found, used = _search_with_fallback(swiggy, q)
            best, qty, note = _pick(found, used, ing.get("grams"))
            if best and best.get("fallback"):
                note = f"check: closest match for “{q}”"
        except swiggy.SwiggyError as e:
            raise ToolError(str(e)) from e
        if not best:
            missing.append(ing.get("name") or q)
            continue
        rows.append({"ingredient": ing.get("name") or q, "need": ing.get("quantity"), "product": best["name"], "brand": best["brand"],
                     "pack": best["pack"], "price": best["price"], "mrp": best["mrp"], "per_100": best["per_100"],
                     "spinId": best["spinId"], "skuId": best["skuId"], "quantity": qty, "note": note, "eta_min": best["eta_min"],
                     "line_total": (best["price"] * qty) if best["price"] is not None else None})
    total = sum(r["line_total"] for r in rows if r["line_total"] is not None)  # code adds, never the model
    unknown = [r["product"] for r in rows if r["price"] is None]
    card = {"type": "cart_plan", "state": "ready", "provider": "Swiggy Instamart", "status": "LIVE", "rows": rows,
            "total": total, "unknown_prices": unknown, "missing": missing, "skipped": skipped,
            "checked": datetime.now(IST).isoformat(timespec="minutes"),
            "note": "Item prices only — delivery/handling fees are shown in your Swiggy cart. Chinku never checks out."}
    ctx["card"](f"🛒 Found {len(rows)} of {len(rows) + len(missing)} ingredients on Swiggy Instamart · items ₹{total:,.0f}"
                + (f" · not found: {', '.join(missing)}" if missing else ""), card)
    return {"items": [{"spinId": r["spinId"], "skuId": r["skuId"], "quantity": r["quantity"]} for r in rows],
            "rows": rows, "total": total, "missing": missing, "skipped": skipped}


def cart_add(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.chinku.adapters import swiggy
    items = [i for i in (inputs.get("items") or []) if isinstance(i, dict) and i.get("spinId")]
    if not items:
        raise ToolError("There is nothing to add to the cart.")
    key = f"icart:{ctx['task'].task_id}:{ctx['step_id']}"
    if not store().claim(key):  # a second "yes" never adds twice
        return {"added": len(items), "duplicate": True}
    try:
        out = swiggy.add_to_cart(items)
    except (swiggy.SwiggyError, PermissionError) as e:
        raise ToolError(str(e)) from e
    cart = out.get("cart") or {}
    pay = ((cart.get("billBreakdown") or {}).get("toPay") or {}).get("value") or cart.get("cartTotalAmount")
    ctx["card"](f"✅ Added {len(items)} items to your Swiggy Instamart cart" + (f" · cart total ₹{pay}" if pay else "")
                + ". Open the Swiggy app to check out and pay — Chinku never pays.",
                {"type": "cart_done", "added": len(items), "in_cart": out.get("items_in_cart"), "to_pay": pay,
                 "lines": [{"label": li.get("label"), "value": li.get("value")} for li in (cart.get("billBreakdown") or {}).get("lineItems") or []
                           if isinstance(li, dict)], "open_url": "https://www.swiggy.com/instamart"})
    return {"added": len(items), "to_pay": pay}


# ---------------- J3: official support contact · promises + follow-up · escalation ----------------
def _domain(url: str | None) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1).lower().removeprefix("www.") if m else "")


def is_official(brand: str | None, url: str | None) -> bool:
    """Code decides 'official': the brand's name must be in the web domain (bajajelectricals.com for Bajaj)."""
    d = _domain(url)
    toks = [t for t in re.findall(r"[a-z0-9]+", (brand or "").lower()) if len(t) > 2]
    return bool(d and toks and any(t in d for t in toks)) and not any(x in d for x in ("justdial", "quora", "reddit", "facebook", "mouthshut",
                                                                                         "consumercomplaints", "sulekha", "youtube"))


class Support(BaseModel):
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    web_form: str | None = None
    source_url: str | None = None
    note: str | None = None


def find_support(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.brain.client import oai
    brand, product = inputs.get("brand"), inputs.get("product") or ""
    if not brand:
        raise ToolError("Which company is it? I couldn't see the brand.")
    r = oai().with_options(timeout=180).responses.create(model=settings().openai_model_brain, tools=[{"type": "web_search"}], reasoning={"effort": "low"}, input=(
        f"Find the OFFICIAL customer-support contact in India for {brand} {product}. Use ONLY {brand}'s own official website "
        "(their own domain). Return ONLY JSON {\"email\", \"phone\", \"whatsapp\", \"web_form\", \"source_url\" (the exact official page), "
        "\"note\"}. null for anything not found on the official site. Never guess an email or number."))
    m = re.search(r"\{.*\}", r.output_text or "", re.S)
    try:
        s = Support.model_validate(json.loads(m.group(0))) if m else Support()
    except Exception:  # noqa: BLE001
        s = Support()
    official = is_official(brand, s.source_url)
    if s.email and not EMAIL_RX.match(s.email.strip()):
        s.email = None
    checked = datetime.now(IST).strftime("%d %b %Y")
    ctx["card"](("✅ Official support found" if official else "⚠️ Support contact found, but NOT on the company's own website")
                + f" · {brand}", {"type": "support", "brand": brand, "product": product, **s.model_dump(), "official": official, "checked": checked})
    return s.model_dump() | {"official": official, "checked": checked, "brand": brand}


ESCALATION = [
    ("Call the helpline again with your ticket number", "call"),
    ("Email support, quoting the ticket number and the promise they made", "email"),
    ("Write to the company's Grievance Officer (listed on their website)", "email"),
    ("National Consumer Helpline: call 1915 or file at consumerhelpline.gov.in (Govt. of India)", "web"),
]


def promise(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """A commitment someone made ('callback by 4 PM', 'technician tomorrow 10-12') → tracked → follow_up at by_when."""
    what = str(inputs.get("what") or "").strip()
    if not what:
        raise ToolError("What did they promise?")
    dt = parse_when(inputs.get("by_when_iso"))
    demo = inputs.get("demo_minutes")
    due = (datetime.now(IST) + timedelta(minutes=float(demo))) if demo else dt  # shortened for the demo — labelled
    pid = "p_" + uuid.uuid4().hex[:8]
    rec = {"kind": "promise", "owner": user, "pid": pid, "who": inputs.get("who") or "they", "what": what,
           "by_when": dt.isoformat(), "due": due.timestamp(), "ticket": inputs.get("ticket"), "evidence_quote": inputs.get("evidence_quote"),
           "contact_phone": inputs.get("contact_phone"), "contact_email": inputs.get("contact_email"), "status": "promised",
           "task_id": ctx["task"].task_id, "demo_shortened": bool(demo)}
    store().put("contacts", f"prom:{pid}", rec)
    ctx["card"](f"🤝 Promised: {rec['who']} — {what} · by {when_words(dt)}" + (" (follow-up shortened for the demo)" if demo else ""),
                {"type": "promise", **rec, "by_words": when_words(dt)})
    return {"pid": pid, "by": when_words(dt), "what": what}


def due_promises(now_ts: float) -> list[dict[str, Any]]:
    return [p for p in store().scan("contacts", kind="promise") if p.get("status") == "promised" and p.get("due", 0) <= now_ts]


def fire_promise(p: dict[str, Any], card_to: Callable[[str, str, dict[str, Any] | None], None]) -> None:
    if not store().claim(f"promfire:{p['pid']}"):
        return
    p["status"] = "follow_up_asked"
    store().put("contacts", f"prom:{p['pid']}", p)
    card_to(p["owner"], f"⏰ Follow-up: {p['who']} promised “{p['what']}” by {when_words(datetime.fromisoformat(p['by_when']))}. "
                        f"Did they do it? (yes / no)", {"type": "followup", **p, "ladder": [x[0] for x in ESCALATION]})


def promise_update(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    pid = inputs.get("pid")
    rows = [p for p in store().scan("contacts", kind="promise") if p.get("owner") == user and p.get("status") in ("promised", "follow_up_asked")]
    p = next((x for x in rows if x["pid"] == pid), None) or (sorted(rows, key=lambda x: x.get("due", 0))[-1] if rows else None)
    if not p:
        raise ToolError("I don't see an open promise to update.")
    done = bool(inputs.get("resolved"))
    p["status"] = "resolved" if done else "not_kept"
    store().put("contacts", f"prom:{p['pid']}", p)
    if done:
        ctx["card"](f"✅ Resolved: {p['what']}", {"type": "promise", **p})
        return {"resolved": True}
    ladder = [x[0] for x in ESCALATION]
    ctx["card"](f"They didn't keep it: {p['what']}. Next steps:", {"type": "escalate", **p, "ladder": ladder})
    return {"resolved": False, "ladder": ladder, "ticket": p.get("ticket")}


# ---------------- J7: scam check (code red flags + model + official sources) ----------------
RED_FLAGS = [
    (r"\b(otp|one time password|pin|cvv)\b", "asks for an OTP / PIN"),
    (r"\b(kyc|pan|aadhaar).{0,40}(update|expire|block|suspend)", "threatens your KYC/account"),
    (r"\b(block(ed)?|suspend(ed)?|deactivat\w*|disconnect\w*)\b.{0,40}\b(today|immediately|24 ?h|tonight)\b", "urgent threat"),
    (r"\b(won|winner|lottery|lucky draw|prize|cashback of)\b", "prize / lottery bait"),
    (r"(bit\.ly|tinyurl|t\.co|is\.gd|cutt\.ly|rb\.gy|wa\.me/\d)", "shortened or WhatsApp link"),
    (r"\b(electricity|power).{0,40}(cut|disconnect)", "electricity disconnection threat (common scam)"),
    (r"\b(customs|parcel|courier).{0,40}(held|seized|pay|fee)", "parcel/customs fee demand"),
    (r"\b(work from home|part[- ]time job|earn ₹?\d+ ?(per|/) ?(day|hour))", "easy-money job offer"),
    (r"\b(anydesk|teamviewer|quick ?support|screen ?share)\b", "asks to install a remote-access app"),
    (r"\b(upi|collect request|scan (the )?qr).{0,40}(receive|get) (money|refund)", "'scan/approve to RECEIVE money' trick"),
]


class Verdict(BaseModel):
    verdict: Literal["Likely scam", "Looks genuine", "Unsure"]
    reasons: list[str] = Field(default_factory=list)
    what_to_do: list[str] = Field(default_factory=list)
    sender: str | None = None
    official_sources: list[str] = Field(default_factory=list)


def scam_check(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.brain.client import oai
    text = str(inputs.get("text") or "")
    if not text and ctx.get("attachments"):
        a = attachment(ctx, inputs.get("attachment"))
        if not a["mime"].endswith("pdf"):
            text = transcribe_image(user, {"attachment": a["id"]}, ctx | {"card": lambda *x: None}).get("text", "")
    if not text.strip():
        raise ToolError("Send me the message text or a screenshot to check.")
    flags = sorted({why for rx, why in RED_FLAGS if re.search(rx, text, re.I)})
    r = oai().with_options(timeout=180).responses.create(model=settings().openai_model_brain, tools=[{"type": "web_search"}], reasoning={"effort": "low"}, input=(
        "You check if a message received in India is a scam. Search official sources (the real company's or bank's own site, "
        "RBI, cybercrime.gov.in, PIB Fact Check) when useful. Return ONLY JSON {\"verdict\": \"Likely scam\"|\"Looks genuine\"|\"Unsure\", "
        "\"reasons\": [short], \"what_to_do\": [short], \"sender\": str|null, \"official_sources\": [urls]}.\nMESSAGE:\n" + text[:3000]))
    m = re.search(r"\{.*\}", r.output_text or "", re.S)
    try:
        v = Verdict.model_validate(json.loads(m.group(0))) if m else Verdict(verdict="Unsure", reasons=["I couldn't check this properly."])
    except Exception:  # noqa: BLE001
        v = Verdict(verdict="Unsure", reasons=["I couldn't check this properly."])
    if v.verdict == "Looks genuine" and len(flags) >= 2:  # code never lets 2+ red flags pass as genuine
        v.verdict = "Unsure"
        v.reasons.insert(0, "It has several common scam signs, so I can't call it genuine.")
    if not v.what_to_do:
        v.what_to_do = ["Don't click links or share OTPs", "Report at cybercrime.gov.in or call 1930"] if v.verdict != "Looks genuine" else []
    ctx["card"](f"🛡️ {v.verdict}", {"type": "scam", **v.model_dump(), "red_flags": flags, "checked": datetime.now(IST).strftime("%d %b %Y"),
                                    "excerpt": text[:300]})
    return v.model_dump() | {"red_flags": flags}


# ---------------- J11: trip planner — itinerary + rated hotels/restaurants → user picks → Chinku calls to book ----------------
class TripPlace(BaseModel):
    name: str
    rating: float | None = None          # e.g. 4.3 (out of 5) as listed publicly
    reviews: str | None = None           # "1.2k reviews"
    price_hint: str | None = None        # "₹2,500/night", "₹600 for two" — as listed, never invented
    phone: str | None = None
    address: str | None = None
    cuisine: str | None = None
    url: str | None = None


class TripDay(BaseModel):
    day: str
    plan: list[str] = Field(default_factory=list)
    travel_note: str | None = None


class Trip(BaseModel):
    title: str = ""
    getting_there: list[str] = Field(default_factory=list)
    itinerary: list[TripDay] = Field(default_factory=list)
    hotels: list[TripPlace] = Field(default_factory=list)
    restaurants: list[TripPlace] = Field(default_factory=list)
    budget_note: str | None = None
    sources: list[str] = Field(default_factory=list)


def trip_plan(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from app.brain.client import oai
    from app.engine.validate import indian_mobile
    dest = inputs.get("destination")
    if not dest:
        raise ToolError("Where do you want to go?")
    days, people = int(inputs.get("days") or 2), int(inputs.get("people") or 2)
    start = inputs.get("start_date")
    r = oai().with_options(timeout=180).responses.create(model=settings().openai_model_brain, tools=[{"type": "web_search"}], reasoning={"effort": "low"}, input=(
        f"Plan a {days}-day trip to {dest} (India) for {people} people"
        + (f" starting {start}" if start else "") + (f", total budget ₹{inputs.get('budget')}" if inputs.get("budget") else "")
        + (f", interests: {inputs.get('interests')}" if inputs.get("interests") else "") + ". Use current web sources. "
        "Return ONLY JSON {\"title\", \"getting_there\": [short options with time], \"itinerary\": [{\"day\": \"Day 1\", \"plan\": [places/"
        "activities in order], \"travel_note\"}], \"hotels\": [up to 6 {\"name\", \"rating\" (number out of 5 as publicly listed), \"reviews\", "
        "\"price_hint\" (as listed), \"phone\" (the hotel's real contact number, null if not found), \"address\", \"url\"}], \"restaurants\": "
        "[up to 6 {\"name\", \"rating\", \"reviews\", \"price_hint\", \"cuisine\", \"phone\", \"address\", \"url\"}], \"budget_note\", \"sources\": "
        "[urls]}. Never invent a phone number, rating or price — null if not found."))
    m = re.search(r"\{.*\}", r.output_text or "", re.S)
    try:
        t = Trip.model_validate(json.loads(m.group(0))) if m else Trip()
    except Exception as e:  # noqa: BLE001
        raise ToolError("I couldn't put the trip plan together — please try again.") from e

    def prep(rows: list[TripPlace], kind: str) -> list[dict[str, Any]]:
        out = []
        for p in sorted(rows, key=lambda x: (x.rating is None, -(x.rating or 0))):  # code ranks by rating; unknown rating last
            from app.engine.validate import phone_kind
            raw = (p.phone or "").strip()
            num, how = phone_kind(raw)  # mobile, landline (0891-2345678) or toll-free — all dialable now
            out.append(p.model_dump() | {"kind": kind, "phone": num or (raw or None), "callable": bool(num), "phone_kind": how})
        return out
    hotels, restaurants = prep(t.hotels, "hotel"), prep(t.restaurants, "restaurant")
    checked = datetime.now(IST).strftime("%d %b %Y")
    card = {"type": "trip", "destination": dest, "days": days, "people": people, "start_date": start, "budget": inputs.get("budget"),
            "title": t.title or f"{days} days in {dest}", "getting_there": t.getting_there, "itinerary": [d.model_dump() for d in t.itinerary],
            "hotels": hotels, "restaurants": restaurants, "budget_note": t.budget_note, "sources": t.sources[:6], "checked": checked}
    ctx["card"](f"🧳 {card['title']} · {people} people · pick hotels/restaurants and I'll call them to book", card)
    places = [p for p in hotels + restaurants if p.get("address")]
    if places:
        try:
            map_(user, {"items": places[:8], "title": f"Hotels and restaurants in {dest}"}, ctx)
        except ToolError:
            pass
    return {"hotels": hotels, "restaurants": restaurants, "itinerary": card["itinerary"], "destination": dest, "people": people,
            "start_date": start, "days": days}


def connect_me(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """J5. Live transfer (OmniDimension 'Custom API transfer') is NOT switched on yet → honest fallback from CLAUDE.md:
    'Talk to them now: call <number>, ticket <id>' with a one-tap dial button."""
    phone = str(inputs.get("phone") or "")
    if not phone:
        raise ToolError("Whom should I connect you to? I need their number.")
    digits = re.sub(r"[^\d+]", "", phone)
    ctx["card"](f"📞 Talk to them now: call {inputs.get('name') or phone}" + (f", ticket {inputs['ticket']}" if inputs.get("ticket") else ""),
                {"type": "connect", "name": inputs.get("name"), "phone": phone, "tel": f"tel:{digits}", "ticket": inputs.get("ticket"),
                 "transfer": "not_available"})
    return {"phone": phone, "ticket": inputs.get("ticket"), "transfer": "not_available"}


# ---------------- Swiggy Food (delivery) + Dineout (tables) — LIVE, user's own login ----------------
def _sw():
    from app.chinku.adapters import swiggy
    if not swiggy.connected():
        raise ToolError("Swiggy isn't connected yet — log in once with your phone + OTP.")
    return swiggy


def food_search(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Real dishes from Swiggy; filters and ranking in CODE (veg, max price, max minutes; cheapest / fastest / best rated)."""
    sw = _sw()
    q = str(inputs.get("query") or "").strip()
    if not q:
        raise ToolError("What would you like to eat?")
    veg, max_price, max_min = inputs.get("veg"), inputs.get("max_price"), inputs.get("max_minutes")
    alts = [a for a in (inputs.get("alternatives") or []) if isinstance(a, str)] or (
        ["veg thali", "veg biryani", "masala dosa", "veg fried rice", "curd rice", "paneer"] if veg else
        ["thali", "biryani", "dosa", "fried rice", "meals", "chicken curry"])
    seen, items, tried = set(), [], []
    for query in [q] + [a for a in alts if a.lower() != q.lower()]:
        try:
            data = sw.call("search_menu", {"query": query, **sw.where("food")}, "food")
        except sw.SwiggyError as e:
            raise ToolError(str(e)) from e
        tried.append(query)
        for it in data.get("items") or []:
            k = (it.get("menu_item_id"), it.get("restaurant_id"))
            if k not in seen:
                seen.add(k)
                items.append(it)
        ok_now = [it for it in items if not (veg and it.get("isVeg") is False) and not (max_price and (it.get("price") or 0) > float(max_price))
                  and not (max_min and (it.get("deliveryTimeMinutes") or 0) > float(max_min))]
        if len(ok_now) >= 3 or len(tried) >= 4:  # enough good options, or 4 searches max (Swiggy: be gentle)
            break
    keep, excluded = [], []
    for it in items:
        name, price, mins = (it.get("name") or "").strip(), it.get("price"), it.get("deliveryTimeMinutes")
        why = ("not veg" if veg and it.get("isVeg") is False else
               f"₹{price} is over ₹{max_price}" if max_price and price is not None and price > float(max_price) else
               f"{mins} min is over {max_min} min" if max_min and mins and mins > float(max_min) else
               "out of stock" if it.get("inStock") == 0 else None)
        row = {"dish": name, "price": price, "veg": it.get("isVeg"), "menu_item_id": it.get("menu_item_id"), "restaurant_id": it.get("restaurant_id"),
               "restaurant": it.get("restaurant_name"), "area": it.get("restaurant_area"), "minutes": mins, "eta": it.get("deliveryTimeRange"),
               "rating": float(it["restaurant_rating"]) if str(it.get("restaurant_rating") or "").replace(".", "").isdigit() else None,
               "ratings": it.get("restaurant_rating_count"), "km": it.get("distanceKm"), "image": it.get("imageUrl")}
        (excluded.append({"item": f"{name} · {row['restaurant']}", "why": why}) if why else keep.append(row))
    prio = inputs.get("priority") if inputs.get("priority") in ("cheapest", "fastest", "best") else "cheapest"
    key = {"cheapest": lambda r: (r["price"] is None, r["price"] or 0, r["minutes"] or 999),
           "fastest": lambda r: (r["minutes"] is None, r["minutes"] or 0, r["price"] or 10**6),
           "best": lambda r: (r["rating"] is None, -(r["rating"] or 0), r["price"] or 10**6)}[prio]
    keep.sort(key=key)
    if not keep:
        raise ToolError(f"Nothing on Swiggy fits right now — I tried {', '.join(tried)}" + (f" (e.g. {excluded[0]['item']}: {excluded[0]['why']})" if excluded else "")
                        + ". Many kitchens close late at night — try a higher budget or later.")
    w = keep[0]
    why = {"cheapest": f"lowest price ₹{w['price']}", "fastest": f"fastest delivery ({w['eta'] or str(w['minutes']) + ' min'})",
           "best": f"best rated restaurant ({w['rating']}★)"}[prio]
    ctx["card"](f"🍽️ Recommended: {w['dish']} · {w['restaurant']} · ₹{w['price']} · {w['eta'] or ''} — {why}",
                {"type": "food", "query": q, "tried": tried, "priority": prio, "why": why, "rows": keep[:6], "excluded": excluded[:6],
                 "address": "your saved Swiggy address", "checked": datetime.now(IST).isoformat(timespec="minutes")})
    return {"rows": keep[:6], "recommended": w, "excluded": excluded}


def food_cart_add(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Add to the Swiggy FOOD cart (after approval) and show Swiggy's real bill. Never places the order, never pays."""
    sw = _sw()
    rid = str(inputs.get("restaurant_id") or "")
    items = [{"menu_item_id": str(i.get("menu_item_id")), "quantity": int(i.get("quantity") or 1)} for i in inputs.get("items") or [] if i.get("menu_item_id")]
    if not (rid and items):
        raise ToolError("Which dish should I add? Pick one from the list first.")
    if not store().claim(f"ifood:{ctx['task'].task_id}:{ctx['step_id']}"):
        return {"added": len(items), "duplicate": True}
    aid = sw.address_id("food")
    try:
        cur = sw.call("get_food_cart", {"addressId": aid}, "food")
        cd = cur.get("data") if isinstance(cur.get("data"), dict) else cur
        cur_rid = str((cd.get("restaurant") or {}).get("id") or cd.get("restaurantId") or "")
        cur_items = [x for x in (cd.get("items") or []) if isinstance(x, dict)]
        if cur_items and cur_rid and cur_rid != rid:
            raise ToolError(f"Your Swiggy food cart already has dishes from {(cd.get('restaurant') or {}).get('name') or 'another restaurant'} — "
                            "I won't replace them. Empty it in the Swiggy app, then ask me again.")
        merged = {i["menu_item_id"]: i for i in items}
        for x in cur_items if cur_rid == rid else []:
            mid = str(x.get("menu_item_id") or "")
            if mid and mid not in merged:
                merged[mid] = {"menu_item_id": mid, "quantity": int(x.get("quantity") or 1)}
        sw.call("update_food_cart", {"restaurantId": rid, "cartItems": list(merged.values()), "addressId": aid}, "food")
        cart = sw.call("get_food_cart", {"addressId": aid, "restaurantName": inputs.get("restaurant") or ""}, "food")
    except sw.SwiggyError as e:
        raise ToolError(str(e)) from e
    cd = cart.get("data") if isinstance(cart.get("data"), dict) else cart
    pr = cd.get("pricing") or {}
    lines = [{"label": l, "value": f"₹{pr[k]:,}" if isinstance(pr.get(k), (int, float)) else pr.get(k)}
             for l, k in (("Item total", "item_total"), ("Delivery", "delivery_charge"), ("Taxes & charges", "taxes_and_charges")) if pr.get(k) is not None]
    pay = pr.get("to_pay")
    issue = (cd.get("delivery_issue") or {})
    oos = [x.get("name") for x in cd.get("items") or [] if isinstance(x, dict) and str(x.get("in_stock")) == "0"]
    warn = " · ".join(w for w in [issue.get("title"), ("Out of stock: " + ", ".join(oos)) if oos else None] if w)
    ctx["card"]((f"⚠️ Added, but {warn.lower()} — you can't order it right now." if warn else "✅ Added to your Swiggy food cart")
                + (f" Swiggy bill: ₹{pay:,}." if isinstance(pay, (int, float)) else "") + " Chinku never places the order or pays.",
                {"type": "cart_done", "added": len(items), "to_pay": pay, "lines": lines, "warning": warn or None,
                 "open_url": "https://www.swiggy.com/checkout", "provider": "Swiggy Food", "restaurant": (cd.get("restaurant") or {}).get("name") or inputs.get("restaurant")})
    return {"added": len(items), "to_pay": pay, "lines": lines}


DINE_LINE = re.compile(r"^\s*\d+\.\s*(?P<name>.+?)\s+—\s+(?P<rest>.*?)\(ID:\s*(?P<id>\d+)\)", re.M)


def _ll(place: str | None, user: str) -> tuple[float, float, str]:
    from app.actions import maps
    area = place or memory.get(user, "home") or memory.get(user, "current location")
    if not area:
        raise ToolError("Which area? (Say “near Kukatpally”, or tell me your home area once.)")
    g = maps.geocode(f"{area}, Hyderabad") or maps.geocode(str(area))
    if not g:
        raise ToolError(f"I couldn't find “{area}” on the map.")
    return round(g[0], 4), round(g[1], 4), str(area)


def dineout_search(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sw = _sw()
    lat, lon, area = _ll(inputs.get("area"), user)
    q = str(inputs.get("query") or "restaurant")
    try:
        d = sw.call("search_restaurants_dineout", {"query": q, "latitude": lat, "longitude": lon}, "dineout")
    except sw.SwiggyError as e:
        raise ToolError(str(e)) from e
    rows = []
    for m in DINE_LINE.finditer(d.get("text") or d.get("_text") or ""):
        rest = m.group("rest")
        rating = re.search(r"(\d(?:\.\d)?)★", rest)
        cost = re.search(r"₹\s?([\d,]+) for two", rest)
        km = re.search(r"([\d.]+) km", rest)
        rows.append({"id": m.group("id"), "name": m.group("name").strip(), "rating": float(rating.group(1)) if rating else None,
                     "cost_for_two": int(cost.group(1).replace(",", "")) if cost else None, "km": float(km.group(1)) if km else None,
                     "about": re.sub(r"\s+", " ", rest).strip(" |")[:120]})
    if inputs.get("max_cost_for_two"):
        rows = [r for r in rows if r["cost_for_two"] is None or r["cost_for_two"] <= float(inputs["max_cost_for_two"])]
    rows.sort(key=lambda r: (r["rating"] is None, -(r["rating"] or 0), r["km"] or 99))  # best rated first (code)
    if not rows:
        raise ToolError(f"No Dineout restaurants found for “{q}” near {area}.")
    ctx["card"](f"🍽️ Tables near {area} · best rated first — pick one and a time",
                {"type": "dineout", "query": q, "area": area, "lat": lat, "lon": lon, "rows": rows[:8], "date": inputs.get("date"),
                 "time": inputs.get("time"), "guests": inputs.get("guests")})
    return {"rows": rows[:8], "lat": lat, "lon": lon, "area": area}


def table_check(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Check the wanted date/time is a real FREE slot (code), before asking for approval."""
    sw = _sw()
    rid = str(inputs.get("restaurant_id") or "")
    if not rid:
        raise ToolError("Which restaurant? Pick one from the list.")
    lat, lon = inputs.get("lat"), inputs.get("lon")
    if lat is None or lon is None:
        lat, lon, _ = _ll(inputs.get("area"), user)
    try:
        when = datetime.fromisoformat(str(inputs.get("when_iso")))
    except (TypeError, ValueError) as e:
        raise ToolError("What date and time should I book?") from e
    when = (when if when.tzinfo else when.replace(tzinfo=IST)).astimezone(IST)
    when = when.replace(minute=(when.minute // 15) * 15, second=0, microsecond=0)
    if when <= datetime.now(IST):
        raise ToolError(f"{when_words(when)} is already past — which time?")
    guests = int(inputs.get("guests") or 2)
    if not 1 <= guests <= 20:
        raise ToolError("Dineout books 1 to 20 guests.")
    try:
        s = sw.call("get_available_slots", {"restaurantId": rid, "date": when.strftime("%Y-%m-%d"), "latitude": lat, "longitude": lon}, "dineout")
    except sw.SwiggyError as e:
        raise ToolError(str(e)) from e
    txt = s.get("_text") or s.get("text") or ""
    day = when.strftime("%Y-%m-%d")
    win = re.search(rf"{day}:(.*)", txt)
    ok = False
    for a, b in re.findall(r"\((\d{1,2}:\d{2} [AP]M)–(\d{1,2}:\d{2} [AP]M)", win.group(1) if win else ""):
        start = datetime.strptime(f"{day} {a}", "%Y-%m-%d %I:%M %p").replace(tzinfo=IST)
        end = datetime.strptime(f"{day} {b}", "%Y-%m-%d %I:%M %p").replace(tzinfo=IST)
        ok = ok or start <= when <= end
    free = re.search(rf"{day} \[FREE\]: slotId=(\d+), itemId=\"([\w-]+)\"", txt)
    paid = re.findall(rf"{day} \[PAID ([^\]]+)\][^\n]*?—\s*\"([^\"]+)\"", txt)
    if not ok:
        raise ToolError(f"{inputs.get('restaurant') or 'They'} have no table at {when_words(when)}. "
                        f"Open times that day: {(win.group(1).strip() if win else 'none listed')}")
    if not free:
        raise ToolError("Only PAID pre-book deals are available then — book that in the Swiggy app (Chinku never pays).")
    params = {"restaurantId": rid, "slotId": int(free.group(1)), "itemId": free.group(2), "reservationTime": int(when.timestamp()),
              "guestCount": guests, "latitude": lat, "longitude": lon}
    return {"params": params, "restaurant": inputs.get("restaurant"), "when": when_words(when), "guests": guests, "free": True,
            "paid_deals": [f"{p} — {d}" for p, d in paid]}


def table_book(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """FREE Dineout reservation only, after approval. Paid deals / payment are never touched."""
    sw = _sw()
    chk = inputs.get("check") or inputs
    p = chk.get("params") or {}
    if not (p.get("itemId") and chk.get("free")):
        raise ToolError("I only book FREE table reservations — paid deals are booked in the Swiggy app.")
    if not store().claim(f"itable:{ctx['task'].task_id}:{ctx['step_id']}"):
        return {"booked": True, "duplicate": True}
    try:
        r = sw.call("book_table", {k: p[k] for k in ("restaurantId", "slotId", "itemId", "reservationTime", "guestCount", "latitude", "longitude")}, "dineout")
    except sw.SwiggyError as e:
        raise ToolError(str(e)) from e
    txt = (r.get("_text") or r.get("text") or json.dumps(r))[:600]
    oid = r.get("orderId") or (re.search(r"[Oo]rder ?ID[:\s]*([\w-]+)", txt).group(1) if re.search(r"[Oo]rder ?ID[:\s]*([\w-]+)", txt) else None)
    booked = bool(oid) and not re.search(r"fail|error|unable|not (be )?booked", txt, re.I)
    ctx["card"](("✅ Table booked" if booked else "⚠️ Swiggy didn't confirm the booking") + f" · {chk.get('restaurant') or ''} · {chk.get('when')} · {chk.get('guests')} guests",
                {"type": "table_booked", "booked": booked, "restaurant": chk.get("restaurant"), "when": chk.get("when"), "guests": chk.get("guests"),
                 "order_id": oid, "note": txt[:240]})
    return {"booked": booked, "order_id": oid}


def grocery_search(user: str, inputs: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """'Show me potato options' — real Instamart products (pack, price, price per kg/L) ranked in CODE; Add buttons."""
    sw = _sw()
    q = str(inputs.get("query") or "").strip()
    if not q:
        raise ToolError("What should I look for on Instamart?")
    try:
        found, used = _search_with_fallback(sw, q)
    except sw.SwiggyError as e:
        raise ToolError(str(e)) from e
    words = [w for w in re.findall(r"[a-z]+", used.lower()) if len(w) > 2]
    rows = [v for v in found if v["rank"] <= 6 and (not words or any(w in (v["name"] or "").lower() for w in words))] or found[:8]
    if inputs.get("max_price"):
        rows = [v for v in rows if v["price"] is not None and v["price"] <= float(inputs["max_price"])]
    prio = inputs.get("priority") if inputs.get("priority") in ("cheapest", "value") else "value"
    key = (lambda v: (v["price"] is None, v["price"] or 0)) if prio == "cheapest" else \
          (lambda v: (v["per_100"] is None, v["per_100"] or 0, v["price"] or 0))
    rows = sorted(rows, key=key)[:6]
    if not rows:
        raise ToolError(f"I couldn't find “{q}” on Instamart right now.")
    loc = "your saved Swiggy address"
    w = rows[0]
    why = f"best value ₹{w['per_100']}/100 g" if prio == "value" and w["per_100"] else f"lowest price ₹{w['price']}"
    ctx["card"](f"🛒 {q.title()} on Swiggy Instamart · recommended {w['name']} {w['pack']} ₹{w['price']} — {why}",
                {"type": "grocery", "query": q, "priority": prio, "why": why, "where": loc, "rows": rows,
                 "checked": datetime.now(IST).isoformat(timespec="minutes")})
    return {"rows": rows, "recommended": w}


REGISTRY: dict[str, Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "grocery_search": grocery_search,
    "food_search": food_search, "food_cart_add": food_cart_add, "dineout_search": dineout_search,
    "table_check": table_check, "table_book": table_book,
    "connect_me": connect_me,
    "trip_plan": trip_plan,
    "find_support": find_support, "promise": promise, "promise_update": promise_update, "scam_check": scam_check,
    "recipe": recipe, "shop_list": shop_list, "cart_add": cart_add,
    "compare": compare,
    "analyze": analyze, "transcribe_image": transcribe_image, "extract": extract, "search": search, "map": map_,
    "draft_email": draft_email, "send_email": send_email, "remind": remind, "remember": remember, "recall": recall,
    "compute": compute, "reply": reply, "call": call,
}
ALWAYS_APPROVE = {"send_email", "call", "cart_add", "food_cart_add", "table_book"}  # anything that leaves the device — enforced in code, whatever the plan says
