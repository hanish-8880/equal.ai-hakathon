"""Live slots for customers who CALL IN (CLAUDE.md §6.2): the plumber's working hours minus slots already locked
by a booking and slots in the past. Code owns availability; the phone agent only reads this list.
The list is pushed into the inbound Omni agent's prompt (on start, every few minutes, and after every booking)
so a caller is only ever offered times that are really free. The final lock is still the conditional write at save time."""
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.jobs import ops
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
_last_sync = {"hash": None}
_lock = threading.Lock()


def _label(start: datetime, end: datetime, now: datetime) -> str:
    d = (start.date() - now.date()).days
    day = "Today" if d == 0 else "Tomorrow" if d == 1 else start.strftime("%A")
    return f"{day} {start.strftime('%d %b')} {start:%H:%M}–{end:%H:%M}"


def work_windows(days: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every working window for the next `days` days (today included), with ids like S1, S2…"""
    s = settings()
    now = now or datetime.now(IST)
    out = []
    for d in range(days if days is not None else s.slot_days):
        base = (now + timedelta(days=d)).date()
        for part in [p.strip() for p in s.work_hours.split(",") if p.strip()]:
            a, b = part.split("-")
            start = datetime.combine(base, datetime.strptime(a, "%H:%M").time(), IST)
            end = datetime.combine(base, datetime.strptime(b, "%H:%M").time(), IST)
            out.append({"start": start.isoformat(), "end": end.isoformat(), "label": _label(start, end, now)})
    return out


def open_slots(plumber: str | None = None, days: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    plumber = plumber or settings().user_whatsapp
    free = ops.free_windows(plumber, work_windows(days, now))
    for i, w in enumerate(free, start=1):
        w["id"] = f"S{i}"
    return free


def agent_prompt(slots: list[dict[str, Any]]) -> str:
    base = Path(__file__).resolve().parents[1].joinpath("brain/prompts/inbound_agent.md").read_text(encoding="utf-8")
    lines = "\n".join(f"- {w['label']}" for w in slots[:8]) or "- (no free slots in the next days — take their preferred time; Hanish will call back)"
    stamp = datetime.now(IST).strftime("%A %d %b, %H:%M IST")
    return base.replace("{plumber_name}", settings().plumber_name) + (
        f"\n\nFREE VISIT SLOTS RIGHT NOW (updated {stamp}) — offer ONLY these, never any other time:\n{lines}\n")


def sync_inbound_agent(force: bool = False) -> bool:
    """Push the current free slots into the inbound agent. Skips the API call when nothing changed."""
    s = settings()
    if not (s.omnidim_inbound_agent_id and s.omnidim_api_key):
        return False
    with _lock:
        slots = open_slots()
        body = agent_prompt(slots)
        h = hashlib.sha1(body.split("(updated")[0].encode() + str([w["label"] for w in slots[:8]]).encode()).hexdigest()
        if h == _last_sync["hash"] and not force:
            return False
        r = httpx.put(f"{s.omnidim_base_url}/agents/{s.omnidim_inbound_agent_id}",
                      json={"context_breakdown": [{"title": "Chinku inbound — plumber + customers", "body": body}]},
                      headers={"Authorization": f"Bearer {s.omnidim_api_key}"}, timeout=30)
        ok = r.status_code < 400
        store().add_event("-", "inbound_slots_synced", {"ok": ok, "status": r.status_code, "slots": [w["label"] for w in slots[:8]]})
        if ok:
            _last_sync["hash"] = h
        return ok


def sync_soon() -> None:
    """After a booking: refresh the agent in the background (never blocks or breaks the booking)."""
    def go():
        try:
            sync_inbound_agent()
        except Exception as e:  # noqa: BLE001
            store().add_event("-", "error", {"where": "slot_sync", "error": str(e)[:300]})
    threading.Thread(target=go, daemon=True).start()
