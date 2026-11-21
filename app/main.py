"""FastAPI app, routes only."""
import json
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app.brain import perceive
from app.channels import whatsapp
from app.engine import runner, steps
from app.engine.validate import mask_phone
from app.store.dynamo import store
from app.voice import omni

app = FastAPI(title="Chinku")


def notify(to: str, text: str, audio: bytes | None = None) -> None:
    if audio:
        whatsapp.send_voice(to, audio, "")
    whatsapp.send(to, text)


steps.NOTIFY = notify
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def reply_to(user: str):
    return lambda text: whatsapp.send(user, text)


def process_whatsapp(msg: dict[str, Any]) -> None:
    user, text, image = msg["from"], msg["body"], None
    store().add_event("-", "wa_in", {"from_last4": user[-4:] if user else None, "body": text[:500],
                                     "media": [m["type"] for m in msg["media"]]})
    try:
        for m in msg["media"]:
            data = whatsapp.download(m["url"])
            if m["type"].startswith("audio"):
                tr = perceive.transcribe(data, "voice." + (m["type"].split("/")[-1] or "ogg"))
                text = f"{text}\n{tr.text}".strip()
                store().add_event("-", "transcribed", {"text": tr.text})
            elif m["type"].startswith("image"):
                image = perceive.read_image(data, m["type"]).model_dump()
                store().add_event("-", "image_read", image)
        runner.handle_message(user, text, image, reply_to(user))
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "whatsapp", "error": str(e)[:500]})
        whatsapp.send(user, "Sorry, oka problem vachindi. Malli try cheyandi.")


@app.post("/webhooks/whatsapp")
async def wa_webhook(request: Request, bg: BackgroundTasks) -> Response:
    msg = whatsapp.parse_inbound(dict(await request.form()))
    if msg["sid"] and store().claim(f"wa:{msg['sid']}"):
        bg.add_task(process_whatsapp, msg)
    return Response(EMPTY_TWIML, media_type="application/xml")


def process_omni(parsed: dict[str, Any]) -> None:
    try:
        runner.on_call_result(parsed, lambda user, text: whatsapp.send(user, text))
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
    mt = {".mp3": "audio/mpeg", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(p.suffix.lower(), "application/octet-stream")
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


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
