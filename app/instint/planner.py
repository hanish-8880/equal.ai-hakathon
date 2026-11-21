"""plan(request, attachments, memory, conversation, active tasks) -> Plan (CLAUDE.md §5)."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.brain.client import json_call, prompt
from app.config import settings
from app.instint import memory
from app.instint.models import Plan
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
LANG = {"en": "English", "te": "Telugu", "hi": "Hindi", "te-en": "Telugu-English mix in English letters"}


def conversation(user: str, n: int = 12) -> list[dict[str, str]]:
    rows = [e for e in store().events(f"chat:{user}") if e["kind"] in ("chat_in", "chat_out", "chat_heard")][-n:]
    return [{"who": "user" if e["kind"] != "chat_out" else "instint", "text": (e["payload"].get("text") or "")[:400]} for e in rows]


def last_outputs(user: str) -> dict[str, Any]:
    d = store().get("contacts", f"ilast:{user}") or {}
    out = {}
    for k, v in (d.get("outputs") or {}).items():
        s = json.dumps(v, ensure_ascii=False)
        out[k] = s[:300] + ("…" if len(s) > 300 else "")
    return out


def active(user: str) -> list[dict[str, Any]]:
    rows = [t for t in store().scan("contacts", kind="itask") if t.get("user") == user and t.get("status") in ("waiting", "running")]
    return [{"task_id": t["task_id"], "request": t["request"][:120], "status": t["status"]} for t in rows[-5:]]


def plan(user: str, request: str, attachments: list[dict[str, Any]], analysis: dict[str, Any] | None = None) -> Plan:
    from app import prefs
    s = settings()
    lang = LANG.get(prefs.get(user).get("language") or "en", "English")
    now = datetime.now(IST)
    payload = {
        "now_ist": now.isoformat(timespec="minutes"), "today": now.strftime("%A %d %B %Y"),
        "request": request or "(no instruction — only an attachment)",
        "attachments": [{"id": a["id"], "type": "pdf" if a["mime"].endswith("pdf") else "photo", "name": a["name"]} for a in attachments],
        "attachment_analysis": analysis, "memory": memory.recall(user), "conversation": conversation(user),
        "last_outputs": last_outputs(user), "active_tasks": active(user),
    }
    sysmsg = prompt("instint_planner").replace("{user_name}", s.user_name).replace("{language}", lang)
    p = json_call(s.openai_model_brain, sysmsg, json.dumps(payload, ensure_ascii=False, default=str), Plan, None, "plan", effort="low")
    store().add_event("-", "instint_plan", {"request": request[:300], "plan": p.model_dump()})
    return p
