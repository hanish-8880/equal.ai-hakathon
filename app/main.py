"""Chinku — FastAPI app, routes only. One router (app/chinku), many channels: the Chinku App, WhatsApp, phone."""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app import prefs
from app.channels import whatsapp
from app.config import settings
from app.engine import runner, steps, validate
from app.engine.validate import mask_phone
from app.chinku import executor, memory
from app.store.dynamo import store
from app.voice import omni

app = FastAPI(title="Chinku")
STARTED_AT = time.time()
IST = timezone(timedelta(hours=5, minutes=30))
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


# ---------------- delivery: app chat (+ WhatsApp when that was the user's last channel) ----------------
def chat_log(user: str, kind: str, text: str, media_url: str | None = None, card: dict[str, Any] | None = None) -> None:
    store().add_event(f"chat:{user}", kind, {"text": text, "media_url": media_url, "card": card})


def _web_preferred(user: str) -> bool:
    c = store().get("contacts", f"chan:{user}")
    return bool(c and c.get("channel") == "web")


def set_channel(user: str, channel: str) -> None:
    store().put("contacts", f"chan:{user}", {"owner": user, "kind": "channel", "channel": channel})


def deliver(to: str, text: str, media_url: str | None = None, card: dict[str, Any] | None = None) -> None:
    """Every message goes to the app chat, in the user's language (numbers guarded in code — app/brain/translate.py)."""
    try:
        from app.brain import translate
        code = translate.code_of(to)
        if code != "en":
            text = translate.to_lang(text, code)
    except Exception as e:  # noqa: BLE001 — never lose a message because translation failed
        store().add_event("-", "error", {"where": "translate", "error": str(e)[:300]})
    chat_log(to, "chat_out", text, media_url, card)
    if not _web_preferred(to):
        try:
            whatsapp.send(to, text, media_url)
        except Exception as e:  # noqa: BLE001 — the app chat already has it
            store().add_event("-", "error", {"where": "wa_send", "error": str(e)[:300]})


def reply_to(user: str):
    return lambda text: deliver(user, text)


def card_to(user: str):
    return lambda text, card: deliver(user, text, None, card)


def notify(to: str, text: str, audio: bytes | None = None) -> None:
    if audio:
        whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
        name = f"{uuid.uuid4().hex}.mp3"
        (whatsapp.MEDIA_DIR / name).write_bytes(audio)
        deliver(to, text, f"{settings().public_base_url}/media/{name}")
        return
    deliver(to, text)


def send_file(to: str, data: bytes, filename: str, caption: str) -> None:
    whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
    (whatsapp.MEDIA_DIR / filename).write_bytes(data)
    deliver(to, caption, f"{settings().public_base_url}/media/{filename}")


steps.NOTIFY = notify
steps.SEND_FILE = send_file


# ---------------- background loops ----------------
def _scheduler_loop() -> None:
    """Calls in flight, retries, reminders, async Chinku steps. State is in the store → survives restarts."""
    while True:
        try:
            runner.tick_schedules()
            runner.sweep_stuck_calls()
            runner.fire_due_retries()
            executor.tick(lambda u, t: deliver(u, t), lambda u, t, c: deliver(u, t, None, c))
        except Exception as e:  # noqa: BLE001
            store().add_event("-", "error", {"where": "scheduler", "error": str(e)[:300]})
        time.sleep(15)


def _wa_poll_loop() -> None:
    """The Twilio sandbox sometimes posts to a stale URL; poll its log and process missed messages (deduped by MessageSid)."""
    while True:
        try:
            for msg in whatsapp.fetch_recent_inbound(STARTED_AT - 120):
                if store().claim(f"wa:{msg['sid']}"):
                    store().add_event("-", "wa_recovered_by_poll", {"sid": msg["sid"], "age_s": round(time.time() - msg["ts"])})
                    threading.Thread(target=process_whatsapp, args=(msg,), daemon=True).start()
        except Exception as e:  # noqa: BLE001
            store().add_event("-", "error", {"where": "wa_poll", "error": str(e)[:300]})
        time.sleep(10)


@app.on_event("startup")
def _start_loops() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    threading.Thread(target=_wa_poll_loop, daemon=True).start()


@app.post("/internal/fire/{task_id}/{step_id}")
def internal_fire(task_id: str, step_id: str, bg: BackgroundTasks) -> dict[str, bool]:
    bg.add_task(runner.fire_schedule, task_id, step_id)
    return {"ok": True}


# ---------------- capture → transcribe → keep attachments → plan ----------------
def _save_media(data: bytes, prefix: str, ext: str) -> str:
    whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
    name = f"{prefix}_{uuid.uuid4().hex[:10]}.{ext}"
    (whatsapp.MEDIA_DIR / name).write_bytes(data)
    return name


def _attachment(data: bytes, mtype: str, n: int) -> dict[str, Any]:
    ext = "pdf" if mtype.endswith("pdf") else {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}.get(mtype.split("/")[-1], "jpg")
    name = _save_media(data, "att", ext)
    return {"id": f"a{n}", "name": name, "mime": "application/pdf" if ext == "pdf" else (mtype if mtype.startswith("image/") else "image/jpeg"),
            "url": f"{settings().public_base_url}/media/{name}", "path": str(whatsapp.MEDIA_DIR / name)}


def process_message(user: str, text: str, media: list[tuple[bytes, str]], source: str = "text") -> None:
    """Shared by the app, WhatsApp and /api/voice."""
    from app.brain import listen
    from app.brain.normalise import normalise
    attachments = []
    try:
        for data, mtype in media:
            if mtype.startswith("audio") or mtype.startswith("video/webm"):
                ext = {"ogg": "ogg", "webm": "webm", "mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "wav": "wav"}.get(mtype.split("/")[-1].split(";")[0], "webm")
                name = _save_media(data, "voice", ext)
                audio_ref = f"{settings().public_base_url}/media/{name}"
                heard = listen.transcribe(data, name, user, source if source != "text" else "voice", audio_ref)
                if heard is None:
                    deliver(user, "I didn't hear anything, try again.")
                    return
                text = f"{text}\n{heard.text}".strip()
                spans = [sp.model_dump() for sp in normalise(heard.text).spans if sp.kind in ("number", "money", "model_code", "phone")]
                store().add_event(f"chat:{user}", "chat_heard", {"text": heard.text, "media_url": audio_ref, "card": None, "spans": spans,
                                                                "low": heard.low_confidence_spans})
            elif mtype.startswith("image") or mtype.endswith("/pdf"):
                attachments.append(_attachment(data, mtype, len(attachments) + 1))
        executor.handle(user, text, attachments, reply_to(user), card_to(user))
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "message", "error": str(e)[:500]})
        deliver(user, "Sorry, something went wrong — please try again.")


# ---------------- WhatsApp (optional channel) ----------------
def process_whatsapp(msg: dict[str, Any]) -> None:
    user = msg["from"]
    store().add_event("-", "wa_in", {"from_last4": user[-4:] if user else None, "body": msg["body"][:500],
                                     "media": [m["type"] for m in msg["media"]]})
    if user != settings().user_whatsapp:  # Chinku works for its owner only
        return
    set_channel(user, "whatsapp")
    chat_log(user, "chat_in", msg["body"] or "(media)")
    media = []
    try:
        for m in msg["media"]:
            media.append((whatsapp.download(m["url"]), m["type"]))
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "wa_media", "error": str(e)[:300]})
    process_message(user, msg["body"], media, source="whatsapp_voice" if any(t.startswith("audio") for _, t in media) else "text")


@app.post("/webhooks/whatsapp")
async def wa_webhook(request: Request, bg: BackgroundTasks) -> Response:
    msg = whatsapp.parse_inbound(dict(await request.form()))
    if msg["sid"] and store().claim(f"wa:{msg['sid']}"):
        bg.add_task(process_whatsapp, msg)
    return Response(EMPTY_TWIML, media_type="application/xml")


# ---------------- phone ----------------
def owner_number(number: str | None) -> str | None:
    owner = settings().user_whatsapp
    if number and owner and validate.indian_mobile(number) == validate.indian_mobile(owner.replace("whatsapp:", "")):
        return owner
    return None


def process_inbound_call(parsed: dict[str, Any]) -> None:
    """The user called Chinku and said what they need: plan it like a typed request (approval still applies)."""
    owner = owner_number(parsed.get("from_number"))
    store().add_event("-", "inbound_call", {"from_last4": (parsed.get("from_number") or "")[-4:], "allowed": bool(owner),
                                            "summary": parsed.get("summary"), "transcript": parsed.get("transcript", "")[:3000]})
    if not owner:
        if parsed.get("transcript"):
            deliver(settings().user_whatsapp, f"📞 Someone else called Chinku ({mask_phone(parsed.get('from_number'))}): "
                                              f"{parsed.get('summary') or 'no summary'}")
        return
    said = "\n".join(ln[5:].strip() for ln in parsed.get("transcript", "").splitlines() if ln.startswith("user:"))
    if not said.strip():
        return
    store().add_event(f"chat:{owner}", "chat_heard", {"text": said, "media_url": None, "card": None, "spans": [], "low": [], "source": "phone"})
    deliver(owner, f"📞 From your call ({mask_phone(parsed.get('from_number'))}):")
    executor.handle(owner, said, [], reply_to(owner), card_to(owner))


def process_omni(parsed: dict[str, Any]) -> None:
    try:
        inbound_id = settings().omnidim_inbound_agent_id
        if parsed.get("direction") == "inbound" or (inbound_id and parsed.get("bot_id") == inbound_id):
            process_inbound_call(parsed)
            return
        runner.on_call_result(parsed, lambda user, text: deliver(user, text))
    except Exception as e:  # noqa: BLE001
        store().add_event(parsed.get("task_id") or "-", "error", {"where": "omni", "error": str(e)[:500]})


@app.post("/webhooks/omni")
async def omni_webhook(request: Request, bg: BackgroundTasks) -> JSONResponse:
    payload = await request.json()
    store().add_event("-", "omni_webhook_raw", {"payload": json.dumps(payload, ensure_ascii=False)[:8000]})
    parsed = omni.parse_webhook(payload)
    if store().claim(f"omni:{parsed.get('call_id') or parsed.get('chinku_call_id')}"):
        bg.add_task(process_omni, parsed)
    return JSONResponse({"ok": True})


@app.get("/media/{name}")
def media(name: str) -> FileResponse:
    p = whatsapp.MEDIA_DIR / Path(name).name
    mt = {".mp3": "audio/mpeg", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".pdf": "application/pdf", ".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
          ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=mt)


# ---------------- the Chinku App (key-protected) ----------------
def _ok(key: str | None) -> bool:
    k = settings().chat_key
    return bool(k) and key == k


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": "forbidden"}, status_code=403)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")


@app.get("/app", response_class=HTMLResponse)
@app.get("/chat", response_class=HTMLResponse)
def app_page(key: str = "") -> Response:
    if not _ok(key):
        return HTMLResponse("Forbidden", status_code=403)
    return HTMLResponse((Path(__file__).parent / "web" / "app.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/chat")
async def chat_send(request: Request, bg: BackgroundTasks) -> JSONResponse:
    form = await request.form()
    if not _ok(form.get("key")):
        return _forbidden()
    owner = settings().user_whatsapp
    text = (form.get("text") or "").strip()
    media, thumbs = [], []
    for f in form.getlist("file"):
        if f is not None and getattr(f, "filename", ""):
            fn = f.filename.lower()
            mtype = f.content_type or ("application/pdf" if fn.endswith(".pdf") else "image/jpeg")
            media.append((await f.read(), mtype))
    set_channel(owner, "web")
    if media:  # show the photo in the user's bubble
        for data, mtype in media:
            if mtype.startswith("image"):
                thumbs.append(f"{settings().public_base_url}/media/{_save_media(data, 'thumb', 'jpg')}")
    chat_log(owner, "chat_in", text, thumbs[0] if thumbs else None, {"type": "sent_media", "count": len(media)} if media else None)
    bg.add_task(process_message, owner, text, media)
    return JSONResponse({"ok": True})


@app.post("/api/voice")
async def voice_send(request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Hold-to-talk (webm/opus or m4a, max 90 s)."""
    form = await request.form()
    if not _ok(form.get("key")):
        return _forbidden()
    f = form.get("file")
    if f is None:
        return JSONResponse({"error": "no audio"}, status_code=400)
    data = await f.read()
    if len(data) > 3_000_000:
        return JSONResponse({"error": "too long — max 90 seconds"}, status_code=413)
    owner = settings().user_whatsapp
    set_channel(owner, "web")
    bg.add_task(process_message, owner, "", [(data, f.content_type or "audio/webm")], "app_voice")
    return JSONResponse({"ok": True})


@app.get("/api/chat/messages")
def chat_messages(key: str = "", since: float = 0) -> JSONResponse:
    if not _ok(key):
        return _forbidden()
    owner = settings().user_whatsapp
    cleared = (store().get("contacts", f"chatclear:{owner}") or {}).get("ts", 0)
    ev = [e for e in store().events(f"chat:{owner}") if e["ts"] > max(since, cleared)]
    return JSONResponse([{"ts": e["ts"], "dir": {"chat_in": "in", "chat_heard": "heard"}.get(e["kind"], "out"), **e["payload"]} for e in ev])


@app.post("/api/chat/clear")
async def chat_clear(request: Request) -> JSONResponse:
    """A fresh conversation view. Nothing is deleted (evidence stays in the store)."""
    form = await request.form()
    if not _ok(form.get("key")):
        return _forbidden()
    owner = settings().user_whatsapp
    store().put("contacts", f"chatclear:{owner}", {"owner": owner, "kind": "chat_clear", "ts": time.time()})
    return JSONResponse({"ok": True})


TOOL_WORDS = {"transcribe_image": "Transcript", "analyze": "Photo read", "send_email": "Email", "draft_email": "Draft",
              "remind": "Reminder", "search": "Research", "map": "Map", "call": "Call", "remember": "Memory", "reply": "Answer",
              "extract": "Details", "compute": "Calculation", "recall": "Memory"}


@app.get("/api/home")
def api_home(key: str = "") -> JSONResponse:
    """Home: Needs you · Active task · Recent work · upcoming reminders (CLAUDE.md §9)."""
    if not _ok(key):
        return _forbidden()
    from app.chinku.tools import when_words
    owner = settings().user_whatsapp
    tasks = sorted((t for t in store().scan("contacts", kind="itask") if t.get("user") == owner), key=lambda t: t.get("updated_at", 0), reverse=True)
    needs, active, recent = [], None, []
    for t in tasks[:40]:
        steps_ = {s["id"]: s for s in (t.get("plan") or {}).get("steps") or []}
        for sid, st in (t.get("steps") or {}).items():
            if st.get("status") == "waiting" and st.get("preview"):
                needs.append({"task_id": t["task_id"], "step_id": sid, "request": t["request"], "preview": st["preview"]})
            elif st.get("status") == "waiting" and (st.get("output") or {}).get("async") and not active:
                active = {"task_id": t["task_id"], "request": t["request"], "what": "Call in progress"}
        if t.get("status") in ("done", "partial", "failed") and len(recent) < 8:
            res = []
            for sid, st in (t.get("steps") or {}).items():
                tool = (steps_.get(sid) or {}).get("tool", "")
                if tool in ("draft_email", "recall", "compute"):
                    continue
                o = st.get("output") or {}
                res.append({"tool": tool, "label": TOOL_WORDS.get(tool, tool), "status": st.get("status"),
                            "detail": o.get("to") or o.get("when") or o.get("title") or o.get("answer", "")[:80] or None})
            recent.append({"task_id": t["task_id"], "request": t["request"], "status": t["status"],
                           "understood": (t.get("plan") or {}).get("understood"), "ts": t.get("updated_at"), "results": res})
    rems = sorted((r for r in store().scan("contacts", kind="reminder") if r.get("owner") == owner and r.get("status") == "scheduled"),
                  key=lambda r: r["due"])
    return JSONResponse({"needs_you": needs[:3], "active": active, "recent": recent,
                         "reminders": [{"text": r["text"], "when": when_words(datetime.fromtimestamp(r["due"], IST)), "repeat": r.get("repeat")}
                                       for r in rems[:5]]})


@app.get("/api/memory")
def api_memory(key: str = "") -> JSONResponse:
    if not _ok(key):
        return _forbidden()
    return JSONResponse(memory.recall(settings().user_whatsapp))


@app.post("/api/memory/forget")
async def api_forget(request: Request) -> JSONResponse:
    form = await request.form()
    if not _ok(form.get("key")):
        return _forbidden()
    return JSONResponse({"ok": memory.forget(settings().user_whatsapp, str(form.get("item") or ""))})


@app.get("/api/calls")
def api_calls(key: str = "") -> JSONResponse:
    if not _ok(key):
        return _forbidden()
    owner = settings().user_whatsapp
    tasks = {t["task_id"]: t for t in store().scan("tasks", user=owner)}
    out = []
    for c in store().scan("calls"):
        if c.get("task_id") not in tasks:
            continue
        ex = {f["name"]: f for f in (c.get("extracted") or {}).get("fields", [])}
        out.append({"call_id": c["call_id"], "ts": c.get("created_at"), "to": mask_phone(c.get("to_number")), "callee": c.get("callee"),
                    "status": c.get("status"), "outcome": c.get("outcome"), "transcript": c.get("transcript"), "fields": ex})
    return JSONResponse(sorted(out, key=lambda x: x["ts"] or 0, reverse=True)[:50])


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    tasks = sorted(store().scan("tasks"), key=lambda t: t["created_at"], reverse=True)
    calls = store().scan("calls")
    for c in calls:
        c["to_number"] = mask_phone(c.get("to_number"))
    return {"tasks": tasks, "calls": calls, "events": {t["task_id"]: store().events(t["task_id"])[-60:] for t in tasks[:10]}}


@app.get("/api/me")
def api_me(key: str = "") -> JSONResponse:
    if not _ok(key):
        return _forbidden()
    s = settings()
    return JSONResponse({"name": s.user_name, "initials": "".join(w[0] for w in s.user_name.split()[:2]).upper(),
                         "language": prefs.get(s.user_whatsapp).get("language"), "languages": prefs.LANGUAGES})


@app.post("/api/me")
async def api_me_set(request: Request) -> JSONResponse:
    form = await request.form()
    if not _ok(form.get("key")):
        return _forbidden()
    try:
        prefs.set_language(settings().user_whatsapp, str(form.get("language")))
    except ValueError:
        return JSONResponse({"error": "unknown language"}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/api/tts")
async def api_tts(request: Request) -> Response:
    """🔊 Listen: read an Chinku answer aloud (OpenAI TTS). Cached per text."""
    import hashlib
    form = await request.form()
    if not _ok(form.get("key")):
        return _forbidden()
    text = str(form.get("text") or "")[:600]
    if not text.strip():
        return JSONResponse({"error": "no text"}, status_code=400)
    name = f"tts_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]}.mp3"
    p = whatsapp.MEDIA_DIR / name
    if not p.exists():
        from app.brain import perceive
        whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
        p.write_bytes(perceive.tts(text))
    return JSONResponse({"url": f"/media/{name}"})


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
