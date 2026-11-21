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
    head = {"flight": f"{req.get('from')} → {req.get('to')} · {day:%a %d %b} · {int(req.get('adults') or 1)} adult(s)"}.get(cat, cat)
    card = {"type": "compare", "category": cat, "title": head, "priority": priority,
            "winner": w.model_dump(), "why": rec.why, "change": rec.what_would_change,
            "rows": [o.model_dump() for o in rec.table[:8]], "excluded": rec.excluded, "providers": [p.name for p in provs],
            "status": sorted({p.status for p in provs}), "failed": failed, "checked": w.fetched_at, "total_found": len(offers)}
    ctx["card"](f"✈️ Recommended ({priority}): {w.item} — "
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
        return swiggy.search(words[0]), words[0]
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
            found, used = _search_with_fallback(swiggy, q)
            best, qty, note = _pick(found, used, ing.get("grams"))
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


REGISTRY: dict[str, Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "recipe": recipe, "shop_list": shop_list, "cart_add": cart_add,
    "compare": compare,
    "analyze": analyze, "transcribe_image": transcribe_image, "extract": extract, "search": search, "map": map_,
    "draft_email": draft_email, "send_email": send_email, "remind": remind, "remember": remember, "recall": recall,
    "compute": compute, "reply": reply, "call": call,
}
ALWAYS_APPROVE = {"send_email", "call", "cart_add"}  # anything that leaves the device — enforced in code, whatever the plan says
