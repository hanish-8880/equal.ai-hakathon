"""The ONLY Twilio code. WhatsApp in/out (never calls)."""
from pathlib import Path
from typing import Any
import uuid

import httpx

from app.config import settings

MEDIA_DIR = Path("media_tmp")
OUTBOX: list[dict[str, Any]] = []  # every send is also recorded here (dashboard + tests)


def parse_inbound(form: dict[str, Any]) -> dict[str, Any]:
    n = int(form.get("NumMedia") or 0)
    media = [{"url": form.get(f"MediaUrl{i}"), "type": form.get(f"MediaContentType{i}", "")} for i in range(n)]
    return {"sid": form.get("MessageSid"), "from": form.get("From"), "body": (form.get("Body") or "").strip(),
            "media": media}


def download(url: str) -> bytes:
    s = settings()
    last: Exception | None = None
    for _ in range(2):
        try:
            r = httpx.get(url, auth=(s.twilio_account_sid, s.twilio_auth_token), follow_redirects=True, timeout=30)
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as e:
            last = e
    raise RuntimeError(f"media download failed: {last}")


def send(to: str, body: str, media_url: str | None = None) -> dict[str, Any]:
    s = settings()
    OUTBOX.append({"to": to, "body": body, "media_url": media_url})
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return {"simulated": True}
    data = {"From": s.twilio_whatsapp_from, "To": to, "Body": body[:1500]}
    if media_url:
        data["MediaUrl"] = media_url
    url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
    last: Exception | None = None
    for _ in range(2):
        try:
            r = httpx.post(url, data=data, auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=20)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            last = e
    raise RuntimeError(f"twilio send failed: {last}")


def send_voice(to: str, audio_mp3: bytes, caption: str = "") -> dict[str, Any]:
    """Voice note: serve the mp3 from our public URL, Twilio fetches it."""
    MEDIA_DIR.mkdir(exist_ok=True)
    name = f"{uuid.uuid4().hex}.mp3"
    (MEDIA_DIR / name).write_bytes(audio_mp3)
    return send(to, caption, f"{settings().public_base_url}/media/{name}")
