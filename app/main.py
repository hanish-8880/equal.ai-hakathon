"""FastAPI app, routes only."""
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app.brain import perceive
from app.channels import whatsapp
from app.engine import runner, steps
from app.jobs import assistant
from app.jobs import store as job_store
from app.config import settings
from app.engine import validate
from app.engine.validate import mask_phone
from app.store.dynamo import store
from app.voice import omni

app = FastAPI(title="Chinku")


# ---------------- delivery: WhatsApp and/or web chat ----------------
def chat_log(user: str, kind: str, text: str, media_url: str | None = None, card: dict[str, Any] | None = None) -> None:
    store().add_event(f"chat:{user}", kind, {"text": text, "media_url": media_url, "card": card})


def _web_preferred(user: str) -> bool:
    c = store().get("contacts", f"chan:{user}")
    return bool(c and c.get("channel") == "web")


def deliver(to: str, text: str, media_url: str | None = None, card: dict[str, Any] | None = None) -> None:
    """Every message is mirrored into the web chat; WhatsApp only if the user's last message came from WhatsApp."""
    chat_log(to, "chat_out", text, media_url, card)
    if not _web_preferred(to):
        whatsapp.send(to, text, media_url)


def notify(to: str, text: str, audio: bytes | None = None) -> None:
    if audio:
        whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
        import uuid
        name = f"{uuid.uuid4().hex}.mp3"
        (whatsapp.MEDIA_DIR / name).write_bytes(audio)
        deliver(to, text, f"{settings().public_base_url}/media/{name}")  # one message: voice note + caption
        return
    deliver(to, text)


steps.NOTIFY = notify


def send_file(to: str, data: bytes, filename: str, caption: str) -> None:
    whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
    (whatsapp.MEDIA_DIR / filename).write_bytes(data)
    deliver(to, caption, f"{settings().public_base_url}/media/{filename}")


steps.SEND_FILE = send_file
assistant.CARD = lambda to, text, card: deliver(to, text, None, card)


def entry(user: str, text: str, image: dict[str, Any] | None, force_confirm: bool = False, heard: Any = None) -> None:
    """One router for every channel (CLAUDE.md v2 §3, §3A)."""
    if settings().mode == "plumber":
        assistant.handle(user, text, image, reply_to(user), force_confirm=force_confirm, heard=heard)
    else:
        runner.handle_message(user, text, image, reply_to(user), force_confirm=force_confirm)


def _scheduler_loop() -> None:
    """Persisted schedules (task state) → survives restarts. EventBridge can call /internal/fire instead."""
    while True:
        try:
            runner.tick_schedules()
            runner.sweep_stuck_calls()
            runner.fire_due_retries()
        except Exception as e:  # noqa: BLE001
            store().add_event("-", "error", {"where": "scheduler", "error": str(e)[:300]})
        time.sleep(15)


STARTED_AT = time.time()


def _wa_poll_loop() -> None:
    """Twilio's sandbox sometimes sends our webhook to a stale URL. Poll Twilio's log every 10 s and process any
    inbound message the webhook missed (deduped by MessageSid, so webhook + poller never double-process)."""
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
def _start_scheduler() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    threading.Thread(target=_wa_poll_loop, daemon=True).start()


@app.post("/internal/fire/{task_id}/{step_id}")
def internal_fire(task_id: str, step_id: str, bg: BackgroundTasks) -> dict[str, bool]:
    bg.add_task(runner.fire_schedule, task_id, step_id)
    return {"ok": True}


EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def reply_to(user: str):
    return lambda text: deliver(user, text)


def set_channel(user: str, channel: str) -> None:
    store().put("contacts", f"chan:{user}", {"owner": user, "kind": "channel", "channel": channel})


def process_whatsapp(msg: dict[str, Any]) -> None:
    user = msg["from"]
    store().add_event("-", "wa_in", {"from_last4": user[-4:] if user else None, "body": msg["body"][:500],
                                     "media": [m["type"] for m in msg["media"]]})
    set_channel(user, "whatsapp")
    chat_log(user, "chat_in", msg["body"] or "(media)")
    media = []
    try:
        for m in msg["media"]:
            media.append((whatsapp.download(m["url"]), m["type"]))
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "wa_media", "error": str(e)[:300]})
    process_message(user, msg["body"], media, source="whatsapp_voice" if any(t.startswith("audio") for _, t in media) else "text")


def _save_audio(data: bytes, mtype: str) -> str:
    """Keep the original audio (evidence + re-transcription, CLAUDE.md §3A step 1)."""
    import uuid
    whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
    ext = {"ogg": "ogg", "webm": "webm", "mpeg": "mp3", "mp4": "m4a", "x-m4a": "m4a", "wav": "wav"}.get(mtype.split("/")[-1].split(";")[0], "webm")
    name = f"voice_{uuid.uuid4().hex[:10]}.{ext}"
    (whatsapp.MEDIA_DIR / name).write_bytes(data)
    return name


def process_message(user: str, text: str, media: list[tuple[bytes, str]], source: str = "text") -> None:
    """Shared by WhatsApp, the Chinku App and /api/voice: capture → transcribe → (normalise → understand → act)."""
    from app.brain import understand as U
    from app.brain.normalise import normalise
    image, heard = None, None
    try:
        for data, mtype in media:
            if mtype.startswith("audio") or mtype.startswith("video/webm"):
                name = _save_audio(data, mtype)
                audio_ref = f"{settings().public_base_url}/media/{name}"
                if settings().mode == "plumber":
                    heard = U.transcribe(data, name, user, source if source != "text" else "voice", audio_ref)
                    if heard is None:
                        deliver(user, "I didn't hear anything, try again.")
                        return
                    said = heard.text
                else:
                    said = perceive.transcribe(data, name).text
                text = f"{text}\n{said}".strip()
                store().add_event("-", "transcribed", {"text": said, "audio_ref": audio_ref})
                spans = [sp.model_dump() for sp in normalise(said).spans if sp.kind in ("number", "money", "model_code", "phone")]
                store().add_event(f"chat:{user}", "chat_heard", {"text": said, "media_url": audio_ref, "card": None, "spans": spans,
                                                                "low": (heard.low_confidence_spans if heard else [])})
            elif mtype.startswith("image"):
                image = perceive.read_image(data, mtype).model_dump()
                store().add_event("-", "image_read", image)
            elif mtype == "application/pdf" or mtype.endswith("/pdf"):
                image = perceive.read_document(data, "document.pdf").model_dump()
                store().add_event("-", "document_read", {k: image.get(k) for k in ("doc_type", "title", "stated_total")}
                                  | {"items": len(image.get("line_items", []))})
        entry(user, text, image, heard=heard)
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "message", "error": str(e)[:500]})
        deliver(user, "Sorry, something went wrong — please try again.")


@app.post("/webhooks/whatsapp")
async def wa_webhook(request: Request, bg: BackgroundTasks) -> Response:
    msg = whatsapp.parse_inbound(dict(await request.form()))
    if msg["sid"] and store().claim(f"wa:{msg['sid']}"):
        bg.add_task(process_whatsapp, msg)
    return Response(EMPTY_TWIML, media_type="application/xml")


def inbound_caller_allowed(number: str | None) -> str | None:
    """Only the owner may give Chinku tasks by phone. Returns the owner's WhatsApp id or None."""
    owner = settings().user_whatsapp
    if number and owner and validate.indian_mobile(number) == validate.indian_mobile(owner.replace("whatsapp:", "")):
        return owner
    return None


def process_inbound_call(parsed: dict[str, Any]) -> None:
    """Someone called Chinku: the inbound agent took the order; run it exactly like a WhatsApp message."""
    owner = inbound_caller_allowed(parsed.get("from_number"))
    store().add_event("-", "inbound_call", {"from_last4": (parsed.get("from_number") or "")[-4:], "allowed": bool(owner),
                                            "summary": parsed.get("summary"), "transcript": parsed.get("transcript", "")[:3000]})
    if not owner:
        return
    said = "\n".join(l[5:].strip() for l in parsed.get("transcript", "").splitlines() if l.startswith("user:"))
    if not said.strip():
        return
    text = f"[Hanish phone call lo cheppadu]\n{said}\n\n[Call summary: {parsed.get('summary') or '-'}]"
    from app.brain.schemas import Heard
    entry(owner, text, None, force_confirm=True, heard=Heard(text=said, source="phone", model="omni-live-asr"))


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
    key = f"omni:{parsed.get('call_id') or parsed.get('chinku_call_id')}"
    if store().claim(key):
        bg.add_task(process_omni, parsed)
    return JSONResponse({"ok": True})


@app.get("/media/{name}")
def media(name: str) -> FileResponse:
    p = whatsapp.MEDIA_DIR / Path(name).name
    mt = {".mp3": "audio/mpeg", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf",
          ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=mt)


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    tasks = sorted(store().scan("tasks"), key=lambda t: t["created_at"], reverse=True)
    calls = store().scan("calls")
    for c in calls:
        c["to_number"] = mask_phone(c.get("to_number"))
    return {"tasks": tasks, "calls": calls,
            "events": {t["task_id"]: store().events(t["task_id"])[-60:] for t in tasks[:10]}}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")


# ---------------- web chat (fallback channel; same engine, same owner identity) ----------------
def _chat_ok(key: str | None) -> bool:
    k = settings().chat_key
    return bool(k) and key == k


@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> Response:
    if not _chat_ok(key):
        return HTMLResponse("Forbidden", status_code=403)
    return HTMLResponse((Path(__file__).parent / "web" / "chat.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, max-age=0"})  # always the latest app


@app.post("/api/chat")
async def chat_send(request: Request, bg: BackgroundTasks) -> JSONResponse:
    form = await request.form()
    if not _chat_ok(form.get("key")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    text = (form.get("text") or "").strip()
    media = []
    f = form.get("file")
    if f is not None and getattr(f, "filename", ""):
        data = await f.read()
        fn = f.filename.lower()
        mtype = f.content_type or ("application/pdf" if fn.endswith(".pdf") else "audio/webm" if fn.endswith((".webm", ".ogg", ".m4a")) else "image/jpeg")
        media.append((data, mtype))
    set_channel(owner, "web")
    chat_log(owner, "chat_in", text or f"(file: {getattr(f, 'filename', '')})")
    bg.add_task(process_message, owner, text, media)
    return JSONResponse({"ok": True})


@app.post("/api/voice")
async def voice_send(request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Chinku App hold-to-talk (webm/opus, max 90 s)."""
    form = await request.form()
    if not _chat_ok(form.get("key")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    f = form.get("file")
    if f is None:
        return JSONResponse({"error": "no audio"}, status_code=400)
    data = await f.read()
    if len(data) > 3_000_000:  # ~90 s of opus
        return JSONResponse({"error": "too long — max 90 seconds"}, status_code=413)
    owner = settings().user_whatsapp
    set_channel(owner, "web")
    bg.add_task(process_message, owner, "", [(data, f.content_type or "audio/webm")], "app_voice")
    return JSONResponse({"ok": True})


@app.get("/api/chat/messages")
def chat_messages(key: str = "", since: float = 0) -> JSONResponse:
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    cleared = (store().get("contacts", f"chatclear:{owner}") or {}).get("ts", 0)
    ev = [e for e in store().events(f"chat:{owner}") if e["ts"] > max(since, cleared)]
    return JSONResponse([{"ts": e["ts"], "dir": {"chat_in": "in", "chat_heard": "heard"}.get(e["kind"], "out"), **e["payload"]} for e in ev])


@app.post("/api/chat/clear")
async def chat_clear(request: Request) -> JSONResponse:
    """Start a fresh conversation view. Nothing is deleted: events stay in the store (evidence / dashboard)."""
    form = await request.form()
    if not _chat_ok(form.get("key")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    store().put("contacts", f"chatclear:{owner}", {"owner": owner, "kind": "chat_clear", "ts": time.time()})
    return JSONResponse({"ok": True})


@app.get("/api/jobs")
def api_jobs(key: str = "") -> JSONResponse:
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    rows = sorted(store().scan("jobs", plumber_id=owner), key=lambda j: j["created_at"], reverse=True)
    return JSONResponse([job_store.card(job_store.Job.model_validate(j)) | {"timeline": j.get("timeline", [])[-20:]} for j in rows])


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
