"""Parts (CLAUDE.md §6.3, §6.4): record shop offers with evidence, compare in code, check the plumber's dependency rule.
Unknown charges stay unknown (never 0). A conditional price applies only if the condition is met. The plumber decides
compatibility of a different model."""
import re
import time
from typing import Any

from app.jobs import ops
from app.jobs import store as jobs
from app.jobs.models import NextAction, Offer
from app.store.dynamo import store

STOCK_WORDS = {"available_now": "in stock now", "tomorrow": "tomorrow", "none": "not in stock"}


def _val(f: dict[str, Any] | None) -> Any:
    return f.get("value") if f and f.get("status") in ("confirmed", "tentative") else None


def _num(v: Any) -> float | None:
    if v is None or v == "" or isinstance(v, bool):  # note: 0 is a real value (free delivery), never "unknown"
        return None
    try:
        return float(str(v).replace(",", "").replace("₹", ""))
    except ValueError:
        return None


def condition_met(condition: str | None, qty: int) -> bool | None:
    """'₹300 each if 10+' → met only if qty ≥ 10. None when there is no condition. Unparseable → not met."""
    if not condition:
        return None
    m = (re.search(r"(\d+)\s*(?:\+|or more|pieces|pcs|nos|units|and above)", condition, re.I)
         or re.search(r"(?:min(?:imum)?|above|over|at least)\s*(\d+)", condition, re.I))
    return qty >= int(m.group(1)) if m else False


def build_offer(shop: str, part_id: str, qty: int, f: dict[str, Any], call_id: str | None) -> Offer:
    exact = _val(f.get("exact_model_available"))
    eta = str(_val(f.get("exact_model_eta")) or "").lower()
    units = _num(_val(f.get("units_available")))
    model = _val(f.get("model_offered"))
    if exact is True:
        stock = "available_now" if (units is None or units >= qty) and not eta else ("tomorrow" if "tomorrow" in eta or "repu" in eta else "available_now" if not eta else "unknown")
    elif exact is False:
        stock = "tomorrow" if "tomorrow" in eta or "repu" in eta else ("none" if not model else "unknown")
    else:
        stock = "unknown"
    o = Offer(shop=shop, part_id=part_id, exact_model_quoted=model,
              matches_requested="exact" if exact is True else "different_model" if exact is False and model else "unknown",
              stock=stock, units_available=int(units) if units is not None else None, unit_price=_num(_val(f.get("unit_price"))),
              mrp=_num(_val(f.get("mrp"))), tax_included={True: "yes", False: "no"}.get(_val(f.get("gst_included")), "unknown"),
              delivery_charge=_num(_val(f.get("delivery_charge"))),
              condition=_val(f.get("price_condition")), hold="offered" if _val(f.get("hold_offered")) is True else "none",
              hold_until=_val(f.get("hold_until")), pickup_address=_val(f.get("pickup_address")), earliest_pickup=_val(f.get("earliest_pickup")),
              source_call_id=call_id, evidence={k: v.get("evidence_quote") for k, v in f.items() if (v or {}).get("evidence_quote")},
              checked_at=time.time())
    o.condition_met = condition_met(o.condition, qty)
    return o


def record_offers(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    job = jobs.get(task["inputs"]["job_id"])
    part_id, qty = task["inputs"]["part_id"], int(float(task["inputs"]["qty"]))
    src = step.get("from_step", "ask_shops")
    calls = {c["callee"].get("name"): c for c in store().scan("calls", task_id=task["task_id"], step_id=src) if not c.get("superseded")}
    new = []
    for r in (task["steps"].get(src) or {}).get("output") or []:
        shop = (r.get("callee") or {}).get("name") or "Shop"
        if r.get("status") != "completed":
            jobs.log(job, "shop_no_answer", f"{shop}: {r.get('outcome') or r.get('status')}", actor="shop", channel="call")
            continue
        o = build_offer(shop, part_id, qty, r.get("fields") or {}, (calls.get(shop) or {}).get("call_id"))
        for old in job.shop_offers:  # a newer call supersedes the older offer from the same shop for the same part
            if old.shop == shop and old.part_id == part_id and not old.superseded_by:
                old.superseded_by = o.source_call_id or "newer"
        job.shop_offers.append(o)
        new.append(o)
        jobs.log(job, "offer_recorded", f"{shop}: {o.exact_model_quoted or '?'} · {o.matches_requested} · {o.stock} · "
                 f"{'₹%g' % o.unit_price if o.unit_price is not None else 'price unknown'}", actor="shop", channel="call", ref=o.source_call_id)
    jobs.save(job)
    return {"status": "done", "output": {"offers": [o.model_dump() for o in new]}}


def rank_offers(offers: list[Offer], qty: int) -> list[dict[str, Any]]:
    """Exact model + in stock first, then lowest known price × qty. Delivery unknown → total unknown."""
    rows = []
    for o in offers:
        price, note = o.unit_price, None
        if o.condition and o.condition_met is False:
            note = f"₹{price:g} only if {o.condition} — not met for {qty}" if price is not None else o.condition
            price = None
        rows.append({"shop": o.shop, "model": o.exact_model_quoted, "match": o.matches_requested, "stock": o.stock,
                     "unit_price": price, "price_note": note, "mrp": o.mrp, "tax": o.tax_included, "delivery": o.delivery_charge,
                     "parts_total": price * qty if price is not None else None,
                     "total": price * qty + o.delivery_charge if price is not None and o.delivery_charge is not None else None,
                     "pickup_address": o.pickup_address, "earliest_pickup": o.earliest_pickup, "hold": o.hold, "hold_until": o.hold_until,
                     "evidence": o.evidence, "call_id": o.source_call_id})
    score = lambda r: (r["match"] == "exact") + (r["stock"] == "available_now")
    rows.sort(key=lambda r: (-score(r), r["parts_total"] if r["parts_total"] is not None else 1e18))
    return rows


def money(v: Any) -> str:
    return "unknown" if v is None else f"₹{v:,.0f}"


def check_dependencies(job: Any, part_id: str, rows: list[dict[str, Any]]) -> tuple[str | None, str]:
    state, msg = None, ""
    part = next((p for p in job.parts_needed if p.part_id == part_id), None)
    for d in job.dependencies:
        if d.part_id != part_id or d.state != "waiting":
            continue
        cand = next((r for r in rows if not d.shop or d.shop.lower() in r["shop"].lower()), None)
        why = []
        if not cand:
            why.append(f"{d.shop} gave no offer")
        else:
            if d.exact_model and cand["match"] != "exact":
                why.append(f"{cand['shop']} has {cand['model'] or 'a different model'}, not {part.model if part and part.model else 'the exact model'}")
            if cand["stock"] != "available_now":
                why.append("exact model only tomorrow" if cand["stock"] == "tomorrow" else "not in stock today")
            if d.max_unit_price is not None and (cand["unit_price"] is None or cand["unit_price"] > d.max_unit_price):
                why.append(f"price {money(cand['unit_price'])} vs your limit {money(d.max_unit_price)}")
        d.state, d.reason = ("met", None) if not why else ("not_met", "; ".join(why))
        state = d.state
        jobs.log(job, "dependency_checked", f"Rule ({d.shop or 'any shop'}, exact={d.exact_model}, ≤{money(d.max_unit_price)}): {d.state}"
                 + (f" — {d.reason}" if d.reason else ""), channel="code")
        if d.state == "met":
            msg = f"\n✅ Your rule is met ({cand['shop']} · {money(cand['unit_price'])}). Next: ask the customer for {d.then}."
            job.next_action = NextAction(what=f"Call the customer to book {d.then}", owner="chinku")
        else:
            job.appointment.state = "pending"
            job.next_action = NextAction(what="Decide on the part", owner="plumber", blocked_by=d.reason)
            other = cand["model"] if cand and cand["model"] else "the other model"
            msg = f"\n⚠️ Your rule is NOT met: {d.reason}. The visit stays pending. Wait for the exact model, take {other}, or check another shop?"
    return state, msg


def compare_offers(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    job = jobs.get(task["inputs"]["job_id"])
    part_id, qty = task["inputs"]["part_id"], int(float(task["inputs"]["qty"]))
    part = next((p for p in job.parts_needed if p.part_id == part_id), None)
    rows = rank_offers([o for o in job.shop_offers if o.part_id == part_id and not o.superseded_by], qty)
    lines = []
    for r in rows:
        match = {"exact": "exact", "different_model": "DIFFERENT model"}.get(r["match"], "model unclear")
        lines.append(f"• {r['shop']}: {r['model'] or '?'} ({match}, {STOCK_WORDS.get(r['stock'], 'stock unclear')}) · {money(r['unit_price'])}/pc"
                     + (f" ({r['price_note']})" if r["price_note"] else "") + f" · delivery {money(r['delivery'])}"
                     + (f" · pickup: {r['pickup_address']}" if r["pickup_address"] else "") + (f", from {r['earliest_pickup']}" if r["earliest_pickup"] else ""))
    state, dep_msg = check_dependencies(job, part_id, rows)
    if state is None:
        job.next_action = NextAction(what=f"Choose a shop for {part.description if part else 'the part'}", owner="plumber")
    jobs.save(job)
    head = f"{job.job_id} · {qty} × {part.description if part else 'part'}{(' ' + part.model) if part and part.model else ''} — shop results:"
    ops.CARD(task["user"], head + "\n" + "\n".join(lines or ["No shop gave an answer."]) + dep_msg,
             {"type": "offers", "job_id": job.job_id, "part": part.model_dump() if part else None, "qty": qty, "rows": rows, "dependency": state})
    return {"status": "done", "output": {"rows": rows, "dependency": state}}


ops.OPS.update({"record_offers": record_offers, "compare_offers": compare_offers})
