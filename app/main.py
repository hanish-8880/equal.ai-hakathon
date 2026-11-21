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
from app.config import settings
from app.engine import validate
from app.engine.validate import mask_phone
from app.store.dynamo import store
from app.voice import omni

app = FastAPI(title="Chinku")


# ---------------- delivery: WhatsApp and/or web chat ----------------
def chat_log(user: str, kind: str, text: str, media_url: str | None = None) -> None:
    store().add_event(f"chat:{user}", kind, {"text": text, "media_url": media_url})


def _web_preferred(user: str) -> bool:
    c = store().get("contacts", f"chan:{user}")
    return bool(c and c.get("channel") == "web")


def deliver(to: str, text: str, media_url: str | None = None) -> None:
    """Every message is mirrored into the web chat; WhatsApp only if the user's last message came from WhatsApp."""
    chat_log(to, "chat_out", text, media_url)
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
    process_message(user, msg["body"], media)


def process_message(user: str, text: str, media: list[tuple[bytes, str]]) -> None:
    """Shared by WhatsApp and the web chat: perceive media, then run the engine."""
    image = None
    try:
        for data, mtype in media:
            m = {"type": mtype}
            if m["type"].startswith("audio"):
                tr = perceive.transcribe(data, "voice." + (m["type"].split("/")[-1] or "ogg"))
                text = f"{text}\n{tr.text}".strip()
                store().add_event("-", "transcribed", {"text": tr.text})
            elif m["type"].startswith("image"):
                image = perceive.read_image(data, m["type"]).model_dump()
                store().add_event("-", "image_read", image)
            elif m["type"] == "application/pdf" or m["type"].endswith("/pdf"):
                image = perceive.read_document(data, "document.pdf").model_dump()
                store().add_event("-", "document_read", {k: image.get(k) for k in ("doc_type", "title", "stated_total")}
                                  | {"items": len(image.get("line_items", []))})
        runner.handle_message(user, text, image, reply_to(user))
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "message", "error": str(e)[:500]})
        deliver(user, "Sorry, something went wrong. Please try again.")


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
    runner.handle_message(owner, text, None, reply_to(owner), force_confirm=True)


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
    return HTMLResponse((Path(__file__).parent / "web" / "chat.html").read_text(encoding="utf-8"))


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
        mtype = f.content_type or ("application/pdf" if f.filename.lower().endswith(".pdf") else "image/jpeg")
        media.append((data, mtype))
    set_channel(owner, "web")
    chat_log(owner, "chat_in", text or f"(file: {getattr(f, 'filename', '')})")
    bg.add_task(process_message, owner, text, media)
    return JSONResponse({"ok": True})


@app.get("/api/chat/messages")
def chat_messages(key: str = "", since: float = 0) -> JSONResponse:
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    ev = [e for e in store().events(f"chat:{owner}") if e["ts"] > since]
    return JSONResponse([{"ts": e["ts"], "dir": "in" if e["kind"] == "chat_in" else "out", **e["payload"]} for e in ev])


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
