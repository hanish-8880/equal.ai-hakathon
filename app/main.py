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
    """Every message is mirrored into the web chat; WhatsApp only if the user's last message came from WhatsApp.
    Shown in the plumber's chosen language (numbers guarded in code — see app/brain/translate.py)."""
    try:
        from app.brain import translate
        code = translate.code_of(to)
        if code != "en":
            text = translate.to_lang(text, code)
    except Exception as e:  # noqa: BLE001 — never lose a message because translation failed
        store().add_event("-", "error", {"where": "translate", "error": str(e)[:300]})
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
from app.jobs import ops as job_ops  # noqa: E402
job_ops.CARD = assistant.CARD
def notify_customer(to: str, text: str, audio: bytes | None = None) -> None:
    """Messages FOR A CUSTOMER always go straight to WhatsApp (never held back by the plumber's app/WhatsApp preference),
    and are recorded so the plumber can see what the customer received."""
    whatsapp.send(to if to.startswith("whatsapp:") else f"whatsapp:{to}", text)
    store().add_event("-", "wa_out_customer", {"to_last4": to[-4:], "text": text[:500]})


job_ops.NOTIFY = notify_customer


def entry(user: str, text: str, image: dict[str, Any] | None, force_confirm: bool = False, heard: Any = None) -> None:
    """One router for every channel (CLAUDE.md v2 §3, §3A)."""
    if settings().mode == "plumber":
        assistant.handle(user, text, image, reply_to(user), force_confirm=force_confirm, heard=heard)
    else:
        runner.handle_message(user, text, image, reply_to(user), force_confirm=force_confirm)


_slot_sync_at = [0.0]


def _scheduler_loop() -> None:
    """Persisted schedules (task state) → survives restarts. EventBridge can call /internal/fire instead."""
    while True:
        try:
            runner.tick_schedules()
            runner.sweep_stuck_calls()
            runner.fire_due_retries()
            if time.time() - _slot_sync_at[0] > 300:  # keep the phone agent's free-slot list fresh (no-op if unchanged)
                _slot_sync_at[0] = time.time()
                from app.jobs import slots
                slots.sync_inbound_agent()
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


EMERGENCY_WORDS = ("flood", "burst", "overflow", "water everywhere", "pipe broke", "leaking a lot", "gushing")


def process_customer(msg: dict[str, Any], media: list[tuple[bytes, str]]) -> None:
    """A customer (not the plumber) messaged the Chinku number: receptionist + estimate from photos (CLAUDE.md feature: WhatsApp receptionist)."""
    from app.jobs import billing
    owner, plumber = settings().user_whatsapp, settings().plumber_name
    sender = msg["from"]
    phone = validate.indian_mobile(sender.replace("whatsapp:", ""))
    text = msg["body"] or ""
    for data, mtype in media:
        if mtype.startswith("audio"):
            text = (text + " " + perceive.transcribe(data, "voice.ogg").text).strip()
    image = next((perceive.read_image(d, t).model_dump() for d, t in media if t.startswith("image")), None)
    job = next((job_store.Job.model_validate(j) for j in store().scan("jobs", plumber_id=owner)
                if j.get("status") not in ("closed", "cancelled") and ((j.get("roles") or {}).get("site_contact") or {}).get("phone") == phone), None)
    if job:
        job_store.log(job, "customer_message", f"Customer: {text[:120] or '(photo)'}", actor="client", channel="whatsapp")
        job_store.save(job)
        if image:
            billing.start_estimate(owner, job, text or "customer photo of the problem", image, from_customer=True)
            whatsapp.send(sender, f"Thank you! {plumber} will look at the photo and send you an estimate shortly. — Chinku")
        else:
            assistant.CARD(owner, f"💬 {job.job_id} customer ({mask_phone(phone)}) says: {text}", job_store.card(job))
            whatsapp.send(sender, f"Got it — I've passed this to {plumber}. — Chinku")
        return
    urgent = any(w in text.lower() for w in EMERGENCY_WORDS)
    reply = (f"Hi! This is Chinku, the assistant of plumber {plumber}. "
             + ("If water is flooding, please turn off the main valve now. " if urgent else "")
             + "Please send a photo of the problem and your address — " + plumber + " will get back to you shortly.")
    whatsapp.send(sender, reply)
    seen = f" Photo: {image.get('fields', {}).get('visible_problem') or image.get('image_type')}." if image else ""
    deliver(owner, f"📩 New enquiry{' — EMERGENCY' if urgent else ''} from {mask_phone(phone)}: {text or '(photo)'}.{seen} Reply “new job …” to take it.")


def process_whatsapp(msg: dict[str, Any]) -> None:
    user = msg["from"]
    store().add_event("-", "wa_in", {"from_last4": user[-4:] if user else None, "body": msg["body"][:500],
                                     "media": [m["type"] for m in msg["media"]]})
    set_channel(user, "whatsapp")
    ph = validate.indian_mobile((user or "").replace("whatsapp:", ""))
    if ph:  # this number has joined the sandbox → Chinku may message it on WhatsApp
        store().put("contacts", f"wajoined:{ph}", {"owner": user, "kind": "wa_joined", "phone": ph})
    chat_log(user, "chat_in", msg["body"] or "(media)")
    media = []
    try:
        for m in msg["media"]:
            media.append((whatsapp.download(m["url"]), m["type"]))
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "wa_media", "error": str(e)[:300]})
    owner = settings().user_whatsapp
    if settings().mode == "plumber" and owner and user != owner:
        try:
            process_customer(msg, media)
        except Exception as e:  # noqa: BLE001
            store().add_event("-", "error", {"where": "customer", "error": str(e)[:300]})
        return
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
    from app.jobs import customer_call, slots
    try:
        windows = slots.work_windows()
        for i, w in enumerate(windows, start=1):
            w["id"] = f"W{i}"
        read = customer_call.read_call(parsed.get("transcript", ""), windows) if parsed.get("transcript") else None
    except Exception as e:  # noqa: BLE001
        read = None
        store().add_event("-", "error", {"where": "customer_call_read", "error": str(e)[:300]})
    # a customer booking (any number — the owner can also test as a customer from his own phone)
    if read and (read.caller_kind == "customer" or (not owner and read.caller_kind != "plumber")):
        customer_call.handle(parsed, read)
        return
    if not owner:  # someone else claiming to be the plumber: never take tasks from them
        if read and read.caller_kind == "plumber":
            deliver(settings().user_whatsapp, f"⚠️ {mask_phone(parsed.get('from_number'))} called claiming to give tasks as you — ignored.")
        return
    said ="\n".join(l[5:].strip() for l in parsed.get("transcript", "").splitlines() if l.startswith("user:"))
    if not said.strip():
        return
    text = f"[Hanish phone call lo cheppadu]\n{said}\n\n[Call summary: {parsed.get('summary') or '-'}]"
    deliver(owner, f"📞 Call received from {mask_phone(parsed.get('from_number'))} — here's what I understood:")
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


@app.get("/app", response_class=HTMLResponse)
@app.get("/chat", response_class=HTMLResponse)
def chat_page(key: str = "") -> Response:
    if not _chat_ok(key):
        return HTMLResponse("Forbidden", status_code=403)
    return HTMLResponse((Path(__file__).parent / "web" / "app.html").read_text(encoding="utf-8"),
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


def _mask_job(j: dict[str, Any]) -> dict[str, Any]:
    """Full job for the app, phones masked except the last 4 digits (CLAUDE.md §14)."""
    for role in ("site_contact", "approver", "billing_to"):
        p = (j.get("roles") or {}).get(role)
        if p and p.get("phone"):
            p["phone"] = mask_phone(p["phone"])
    for a in j.get("extra_work") or []:
        if (a.get("sent_to") or {}).get("phone"):
            a["sent_to"]["phone"] = mask_phone(a["sent_to"]["phone"])
    return j


@app.get("/api/jobs/{job_id}/pdf")
def api_job_pdf(job_id: str, kind: str = "invoice", key: str = "") -> Response:
    """Download the invoice / estimate PDF (built from the saved job, so drafts and old bills always work)."""
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    from app.jobs import billing
    job = job_store.get(job_id)
    if not job or job.plumber_id != settings().user_whatsapp or kind not in ("invoice", "estimate"):
        return JSONResponse({"error": "not found"}, status_code=404)
    inv = job.invoice if kind == "invoice" else job.estimate
    if not inv or inv.state in (None, "none"):
        return JSONResponse({"error": f"no {kind} for {job_id}"}, status_code=404)
    data = billing._pdf(job, inv, kind, billing.pricebook(job.plumber_id)[1])
    name = f"{inv.number or (job.job_id + '-' + kind + '-draft')}.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/jobs")
def api_jobs(key: str = "") -> JSONResponse:
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    rows = sorted(store().scan("jobs", plumber_id=owner), key=lambda j: j["created_at"], reverse=True)
    out = [job_store.card(job_store.Job.model_validate(j)) | {"timeline": j.get("timeline", [])[-30:], "full": _mask_job(j)} for j in rows]
    try:  # next steps in the plumber's language (cached; numbers guarded)
        from app.brain import translate
        code = translate.code_of(owner)
        if code != "en":
            open_ = [c for c in out if c.get("status") not in ("closed", "cancelled")][:12]
            na = [c["next_action"] for c in open_ if c.get("next_action")]
            tr = translate.many([x.get("what") for x in na] + [x.get("blocked_by") for x in na], code)
            for x in na:
                for k in ("what", "blocked_by"):
                    if x.get(k):
                        x[k] = tr.get(x[k], x[k])
    except Exception as e:  # noqa: BLE001
        store().add_event("-", "error", {"where": "translate_jobs", "error": str(e)[:300]})
    return JSONResponse(out)


@app.get("/api/calls")
def api_calls(key: str = "") -> JSONResponse:
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    owner = settings().user_whatsapp
    tasks = {t["task_id"]: t for t in store().scan("tasks", user=owner)}
    out = []
    for c in store().scan("calls"):
        t = tasks.get(c.get("task_id"))
        if not t:
            continue
        ex = {f["name"]: f for f in (c.get("extracted") or {}).get("fields", [])}
        out.append({"call_id": c["call_id"], "ts": c.get("created_at"), "to": mask_phone(c.get("to_number")), "callee": c.get("callee"),
                    "status": c.get("status"), "outcome": c.get("outcome"), "attempt": c.get("attempt", 0), "spec": t.get("spec"),
                    "job_id": (t.get("inputs") or {}).get("job_id"), "transcript": c.get("transcript"), "fields": ex,
                    "goal": ((c.get("brief") or {}).get("goal")), "simulated": False})
    return JSONResponse(sorted(out, key=lambda x: x["ts"] or 0, reverse=True)[:50])


@app.get("/api/me")
def api_me(key: str = "") -> JSONResponse:
    if not _chat_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    from app.jobs import prefs
    s = settings()
    p = prefs.get(s.user_whatsapp)
    return JSONResponse({"name": s.plumber_name, "initials": "".join(w[0] for w in s.plumber_name.split()[:2]).upper(),
                         "language": p.get("language"), "languages": prefs.LANGUAGES})


@app.post("/api/me")
async def api_me_set(request: Request) -> JSONResponse:
    form = await request.form()
    if not _chat_ok(form.get("key")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    from app.jobs import prefs
    try:
        prefs.set_language(settings().user_whatsapp, str(form.get("language")))
    except ValueError:
        return JSONResponse({"error": "unknown language"}, status_code=400)
    return JSONResponse({"ok": True})


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
