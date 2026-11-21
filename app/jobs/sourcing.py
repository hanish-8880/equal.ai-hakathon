"""Start part sourcing for a job (CLAUDE.md §6.3/§6.4): read the part, the shops, and the plumber's rule; then call shops.
Never guess a model. Shops come from the plumber's contacts; map shops are shown with address/map link and called only
after the plumber picks them and gives a number."""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.brain.client import json_call, prompt
from app.config import settings
from app.engine import runner
from app.engine.validate import indian_mobile, mask_phone
from app.jobs import ops
from app.jobs import store as jobs
from app.jobs.models import Dependency, Job, NextAction, Part
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))


class DepDraft(BaseModel):
    shop: str | None = None
    exact_model: bool = True
    max_unit_price: float | None = None
    then_label: str | None = None  # e.g. "today 4–6 pm"


class PartDraft(BaseModel):
    description: str | None = None
    brand: str | None = None
    model: str | None = None  # EXACTLY as written/spoken; null if not given or unreadable
    size: str | None = None
    qty: int | None = None
    shops: list[str] = Field(default_factory=list)  # shop names as the plumber said them
    model_unreadable: bool = False
    dependency: DepDraft | None = None


def extract_part(text: str, image: dict[str, Any] | None) -> PartDraft:
    payload = json.dumps({"now_ist": datetime.now(IST).isoformat(), "message": text, "photo_reading": image}, ensure_ascii=False)
    return json_call(settings().openai_model_brain, prompt("part_extract"), payload, PartDraft, None, "part_extract")


def _resolve_shops(user: str, names: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    contacts = [c for c in store().scan("contacts", user=user) if c.get("alias") and c.get("phone")]
    found, missing = [], []
    for n in names:
        key = n.lower().replace("shop", "").replace("hardware", "").replace("sanitary", "").strip()
        c = next((c for c in contacts if key and (key in c["alias"] or c["alias"] in key)), None)
        (found.append({"name": n.strip().title(), "phone": c["phone"]}) if c else missing.append(n))
    return found, missing


def _nearby_card(user: str, job: Job) -> None:
    """Background: shops near the job site from OpenStreetMap (address, distance, map link)."""
    from app.actions import maps
    place = ", ".join(str(x) for x in (job.site_address.value, "Hyderabad") if x)
    try:
        g = maps.geocode(place) or maps.geocode("Hyderabad")
        rows = maps.nearby_shops(g[0], g[1]) if g else []
    except Exception as e:  # noqa: BLE001
        store().add_event(job.job_id, "maps_error", {"error": str(e)[:200]})
        return
    if rows:
        ops.CARD(user, f"🗺️ Plumbing / hardware shops near {job.site_address.value or 'the site'} (OpenStreetMap — tap for the map). "
                       f"Say “call <shop> <number>” to add one.",
                 {"type": "shops", "job_id": job.job_id, "rows": rows})


def start_sourcing(user: str, job: Job, text: str, image: dict[str, Any] | None, send) -> str | None:
    d = extract_part(text, image)
    if d.model_unreadable or not d.model:
        jobs.log(job, "part_model_missing", "Model not readable / not given — asked the plumber", channel="app")
        jobs.save(job)
        return f"{job.job_id}: which exact model is it? Send a clear photo of the label or type the model code — I won't guess it."
    qty = d.qty or 1
    part = Part(part_id="P" + uuid.uuid4().hex[:5], description=d.description or "part", brand=d.brand, model=d.model, size=d.size, qty=qty)
    job.parts_needed.append(part)
    if d.dependency and (d.dependency.shop or d.dependency.max_unit_price):
        job.dependencies.append(Dependency(part_id=part.part_id, shop=d.dependency.shop, exact_model=d.dependency.exact_model,
                                           max_unit_price=d.dependency.max_unit_price, then=d.dependency.then_label or "the visit"))
    shops, missing = _resolve_shops(user, d.shops)
    desc = " ".join(x for x in (part.brand, part.description, part.model, part.size) if x)
    jobs.log(job, "part_requested", f"{qty} × {desc}" + (f" · rule: {d.dependency.shop} ≤ ₹{d.dependency.max_unit_price:g}" if d.dependency and d.dependency.max_unit_price else ""),
             actor="plumber")
    threading.Thread(target=_nearby_card, args=(user, job), daemon=True).start()
    if not shops:
        job.next_action = NextAction(what=f"Shop numbers needed for {desc}", owner="plumber", blocked_by="no shop numbers")
        jobs.save(job)
        names = ", ".join(missing) or "your shops"
        return (f"{job.job_id}: part noted — {qty} × {desc}. I don't have numbers for {names}. "
                f"Send “contact <shop> <number>”, or pick from the nearby shops I'm loading.")
    job.next_action = NextAction(what=f"Calling {', '.join(s['name'] for s in shops)} about {desc}", owner="chinku")
    jobs.save(job)
    runner.start_spec(user, "part_sourcing", {"job_id": job.job_id, "part_id": part.part_id, "part_desc": desc, "qty": qty,
                                              "shops": shops, "plumber_name": settings().plumber_name}, send)
    extra = f" (no number yet for {', '.join(missing)})" if missing else ""
    ops.CARD(user, f"📞 Calling {', '.join(s['name'] + ' ' + mask_phone(s['phone']) for s in shops)} about {qty} × {desc}{extra}.", jobs.card(job))
    return None
