"""Price list, estimates and invoices (CLAUDE.md §6.6 + estimate flow).
Two-pass money: the LLM only LISTS lines; code prices, sums, excludes returned parts and reconciles against approvals and a
spoken total. Nothing is sent without the plumber's "yes"; sends are idempotent (one invoice number, never duplicated)."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.brain.client import json_call, prompt
from app.config import settings
from app.engine.validate import mask_phone
from app.jobs import ops
from app.jobs import prefs
from app.jobs import store as jobs
from app.jobs.models import Invoice, InvoiceLine, Job, NextAction
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))

# DEMO price list — clearly labelled; the plumber replaces it with his own rates in settings.
DEMO_PRICEBOOK: list[dict[str, Any]] = [
    {"item": "Angle valve", "aliases": ["angle valve", "angle cock"], "unit": "pc", "rate": 350},
    {"item": "Connection hose", "aliases": ["hose", "connection pipe", "flexible pipe"], "unit": "pc", "rate": 300},
    {"item": "PVC / bottle trap", "aliases": ["trap", "bottle trap", "pvc trap"], "unit": "pc", "rate": 250},
    {"item": "Ball valve", "aliases": ["ball valve"], "unit": "pc", "rate": 450},
    {"item": "Health faucet", "aliases": ["health faucet", "jet spray"], "unit": "pc", "rate": 800},
    {"item": "Pillar cock / tap", "aliases": ["pillar cock", "tap", "faucet"], "unit": "pc", "rate": 900},
    {"item": "Bib cock", "aliases": ["bib cock"], "unit": "pc", "rate": 450},
    {"item": "Teflon tape", "aliases": ["teflon", "tape"], "unit": "pc", "rate": 30},
    {"item": "Elbow / tee fitting", "aliases": ["elbow", "tee", "reducer", "union"], "unit": "pc", "rate": 50},
    {"item": "Flush tank repair", "aliases": ["flush tank", "cistern"], "unit": "job", "rate": 600},
    {"item": "Geyser service", "aliases": ["geyser", "water heater", "descaling"], "unit": "job", "rate": 700},
    {"item": "Leak repair", "aliases": ["leak", "leakage"], "unit": "job", "rate": 400},
    {"item": "Labour", "aliases": ["labour", "labor"], "unit": "hour", "rate": 400},
    {"item": "Visit charge", "aliases": ["visit"], "unit": "visit", "rate": 200},
]


def pricebook(user: str) -> tuple[list[dict[str, Any]], bool]:
    own = prefs.get(user).get("pricebook")
    return (own, False) if own else (DEMO_PRICEBOOK, True)


def lookup(item: str, book: list[dict[str, Any]]) -> dict[str, Any] | None:
    low = (item or "").lower()
    best = None
    for e in book:
        for a in e["aliases"]:
            if a in low and (best is None or len(a) > best[0]):
                best = (len(a), e)
    return best[1] if best else None


def inr(v: float) -> str:
    return f"₹{v:,.0f}" if float(v).is_integer() else f"₹{v:,.2f}"


# ---------------- schemas (LLM lists, code computes) ----------------
class LineDraft(BaseModel):
    item: str
    qty: float = 1
    unit_rate: float | None = None       # only if the plumber SAID a per-piece rate
    amount_spoken: float | None = None   # only if he said an amount for the whole line
    returned: bool = False               # unused / returned parts are never billed
    evidence_quote: str | None = None


class InvoiceDraftOut(BaseModel):
    lines: list[LineDraft] = Field(default_factory=list)
    labour_amount: float | None = None
    labour_hours: float | None = None
    labour_evidence: str | None = None
    spoken_total: float | None = None
    send_to_hint: str | None = None


class EstPart(BaseModel):
    item: str
    qty: float = 1
    why: str | None = None


class EstimateDraft(BaseModel):
    problem: str
    urgency: str = "routine"  # emergency | routine
    likely_parts: list[EstPart] = Field(default_factory=list)
    labour_hours: float = 1
    confidence: float = 0.5
    questions: list[str] = Field(default_factory=list)


# ---------------- invoice ----------------
def compute_invoice(user: str, job: Job, d: InvoiceDraftOut) -> tuple[Invoice, list[str], list[str]]:
    """Code-only pricing + reconciliation. Returns (invoice, discrepancies, excluded)."""
    book, demo = pricebook(user)
    lines, issues, excluded = [], [], []
    for ln in d.lines:
        if ln.returned:
            excluded.append(f"{ln.qty:g} × {ln.item} (returned / unused)")
            continue
        if ln.unit_rate is not None:
            rate, src = ln.unit_rate, f"you said: “{ln.evidence_quote or ln.unit_rate}”"
        elif ln.amount_spoken is not None and ln.qty:
            rate, src = ln.amount_spoken / ln.qty, f"you said: “{ln.evidence_quote or ln.amount_spoken}”"
        else:
            e = lookup(ln.item, book)
            if not e:
                issues.append(f"no rate for “{ln.item}” — tell me the price")
                continue
            rate, src = e["rate"], f"price list{' (demo)' if demo else ''}"
        lines.append(InvoiceLine(item=ln.item, qty=ln.qty, unit_rate=rate, amount=round(ln.qty * rate, 2), source=src))
    if d.labour_amount is not None:
        labour = d.labour_amount
    elif d.labour_hours is not None:
        e = lookup("labour", book)
        labour = d.labour_hours * (e["rate"] if e else 0)
    else:
        labour = 0.0
        issues.append("labour not mentioned — no labour charged")
    # reconcile with approvals: extra work on the bill must be approved by the designated approver
    for ln in lines:
        for a in job.extra_work:
            if any(w in ln.item.lower() for w in a.scope.lower().split() if len(w) > 3):
                if a.response != "approved":
                    issues.append(f"{ln.item} {inr(ln.amount)} — extra work is {a.response}, not approved")
                elif a.approved_amount is not None and abs(a.approved_amount - ln.amount) > 0.5:
                    issues.append(f"{ln.item}: approved {inr(a.approved_amount)} but billing {inr(ln.amount)}")
    tax_rate = float(prefs.get(user).get("tax_rate") or 0)
    sub = sum(x.amount for x in lines) + labour
    total = round(sub * (1 + tax_rate / 100), 2)
    if d.spoken_total is not None and abs(d.spoken_total - total) > 0.5:
        issues.append(f"you said total {inr(d.spoken_total)} but the lines add up to {inr(total)}")
    inv = Invoice(lines=lines, labour=labour, tax_rate=tax_rate, total=total, state="awaiting_plumber",
                  date=datetime.now(IST).strftime("%d %b %Y"))
    inv.idempotency_key = hashlib.sha1(json.dumps([x.model_dump() for x in lines] + [labour, total], sort_keys=True).encode()).hexdigest()[:12]
    return inv, issues, excluded


def readback(job: Job, inv: Invoice, issues: list[str], excluded: list[str], kind: str = "invoice") -> str:
    parts = [f"{x.qty:g} {x.item} {inr(x.amount)}" for x in inv.lines]
    if inv.labour:
        parts.append(f"labour {inr(inv.labour)}")
    rec = job.roles.billing_to
    to = (rec.name or rec.email) if rec else None
    who = (f"{to}" + (f" against {job.roles.work_order}" if job.roles.work_order else "")) if to else "the customer"
    txt = f"{job.job_id}: " + ", ".join(parts) + f". Total {inr(inv.total)}."
    if excluded:
        txt += " Not billed: " + "; ".join(excluded) + "."
    if issues:
        txt += "\n⚠️ Check: " + "; ".join(issues) + "."
    return txt + (f"\nSend to {who}? (yes / correct it)" if kind == "invoice" else "\nSend this estimate to the customer? (yes / correct it)")


def _card(job: Job, inv: Invoice, kind: str, demo: bool, pdf_url: str | None = None) -> dict[str, Any]:
    return {"type": kind, "job_id": job.job_id, "number": inv.number, "state": inv.state, "lines": [x.model_dump() for x in inv.lines],
            "labour": inv.labour, "tax_rate": inv.tax_rate, "total": inv.total, "pdf_url": pdf_url, "demo_rates": demo,
            "bill_to": (job.roles.billing_to.name or job.roles.billing_to.email) if job.roles.billing_to else None, "wo": job.roles.work_order}


def start_invoice(user: str, job: Job, text: str, previous: dict[str, Any] | None = None) -> None:
    payload = json.dumps({"closing_note": text, "previous_draft": previous}, ensure_ascii=False)
    d = json_call(settings().openai_model_brain, prompt("invoice_extract"), payload, InvoiceDraftOut, job.job_id, "invoice_extract")
    inv, issues, excluded = compute_invoice(user, job, d)
    inv.number = job.invoice.number  # keep the number if one was already issued
    job.invoice = inv
    job.status = "invoicing"
    job.next_action = NextAction(what=f"Confirm invoice {inr(inv.total)}", owner="plumber", blocked_by="; ".join(issues) or None)
    jobs.log(job, "invoice_drafted", f"Draft {inr(inv.total)} ({len(inv.lines)} lines)" + (f"; issues: {'; '.join(issues)}" if issues else ""), actor="plumber")
    jobs.save(job)
    store().put("contacts", f"pendingbill:{user}", {"owner": user, "job_id": job.job_id, "kind": "invoice", "draft": d.model_dump(), "ts": time.time()})
    _, demo = pricebook(user)
    ops.CARD(user, readback(job, inv, issues, excluded), _card(job, inv, "invoice", demo))


def _pdf(job: Job, inv: Invoice, kind: str, demo: bool) -> bytes:
    from fpdf import FPDF
    s = settings()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, f"{s.plumber_name} Plumbing Services", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"{'TAX INVOICE' if kind == 'invoice' else 'ESTIMATE (final price after inspection)'}  |  No: {inv.number or '-'}  |  Date: {inv.date}",
             new_x="LMARGIN", new_y="NEXT")
    bt = job.roles.billing_to
    pdf.cell(0, 6, f"Bill to: {(bt.name or '') if bt else ''} {(bt.email or '') if bt else ''}".strip(), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Site: {job.flat.value or ''} {job.site_address.value or ''}  |  Job: {job.job_id}"
             + (f"  |  WO: {job.roles.work_order}" if job.roles.work_order else ""), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    for w, h in ((90, "Item"), (20, "Qty"), (35, "Rate (Rs.)"), (35, "Amount (Rs.)")):
        pdf.cell(w, 8, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    for x in inv.lines:
        pdf.cell(90, 8, x.item[:48], border=1); pdf.cell(20, 8, f"{x.qty:g}", border=1)
        pdf.cell(35, 8, f"{x.unit_rate:,.0f}", border=1, align="R"); pdf.cell(35, 8, f"{x.amount:,.0f}", border=1, align="R"); pdf.ln()
    if inv.labour:
        pdf.cell(145, 8, "Labour", border=1); pdf.cell(35, 8, f"{inv.labour:,.0f}", border=1, align="R"); pdf.ln()
    if inv.tax_rate:
        pdf.cell(145, 8, f"Tax {inv.tax_rate:g}%", border=1); pdf.cell(35, 8, "", border=1); pdf.ln()
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(145, 9, "TOTAL", border=1); pdf.cell(35, 9, f"{inv.total:,.0f}", border=1, align="R"); pdf.ln(12)
    pdf.set_font("Helvetica", "", 8)
    if demo:
        pdf.multi_cell(0, 5, "Rates marked 'price list' come from a DEMO price list for this prototype; the plumber sets real prices in settings.",
                       new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(0, 5, "Prepared by Chinku (AI office assistant). Totals computed in code.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _deliver(user: str, job: Job, inv: Invoice, kind: str, demo: bool) -> tuple[str, list[dict[str, Any]]]:
    """Idempotent sends: each (document number, channel) is claimed once — retries never duplicate."""
    from app.actions import email as email_action
    from app.channels import whatsapp
    data = _pdf(job, inv, kind, demo)
    name = f"{inv.number}.pdf"
    whatsapp.MEDIA_DIR.mkdir(exist_ok=True)
    (whatsapp.MEDIA_DIR / name).write_bytes(data)
    url = f"{settings().public_base_url}/media/{name}"
    results = []
    bt, sc = job.roles.billing_to, job.roles.site_contact
    targets = []
    if kind == "invoice" and bt and bt.email:
        targets.append(("email", bt.email))
    if sc and sc.phone and ops.wa_joined(sc.phone):
        targets.append(("whatsapp", sc.phone))
    for ch, to in targets:
        if not store().claim(f"docsend:{inv.number}:{inv.idempotency_key}:{ch}:{to}"):
            results.append({"channel": ch, "to": to, "status": "already_sent"})
            continue
        try:
            if ch == "email":
                used = email_action.send(to, f"{'Invoice' if kind == 'invoice' else 'Estimate'} {inv.number} — {job.job_id}",
                                         f"Please find the {kind} for {job.job_id} attached. Total: Rs. {inv.total:,.0f}.\n\n— {settings().plumber_name}",
                                         (data, "application/pdf"), filename=name)
                results.append({"channel": "email", "to": to, "delivered_to": used, "status": "sent"})
            else:
                ops.NOTIFY(f"whatsapp:{to}", f"{'Invoice' if kind == 'invoice' else 'Estimate'} {inv.number} from {settings().plumber_name}: "
                                             f"total Rs. {inv.total:,.0f}. PDF: {url}", None)
                results.append({"channel": "whatsapp", "to": mask_phone(to), "status": "sent"})
        except Exception as e:  # noqa: BLE001 — report, never silence
            results.append({"channel": ch, "to": to, "status": "failed", "error": str(e)[:200]})
    return url, results


def send_pending(user: str, send) -> bool:
    """Plumber said yes to a pending invoice / estimate."""
    p = store().get("contacts", f"pendingbill:{user}")
    if not p or not p.get("job_id"):
        return False
    job = jobs.get(p["job_id"])
    kind = p["kind"]
    inv = job.invoice if kind == "invoice" else job.estimate
    if not inv.number:
        seq = sum(1 for j in jobs.open_jobs(user)) + int(time.time()) % 1000
        inv.number = f"{'INV' if kind == 'invoice' else 'EST'}-{datetime.now(IST).strftime('%y%m%d')}-{job.job_id}-{seq}"
    inv.state = "approved"
    _, demo = pricebook(user)
    url, results = _deliver(user, job, inv, kind, demo)
    inv.send_attempts.append({"ts": time.time(), "results": results})
    sent = [r for r in results if r["status"] in ("sent", "already_sent")]
    failed = [r for r in results if r["status"] == "failed"]
    inv.state = "sent" if sent and not failed else ("delivery_failed" if failed else "approved")
    if kind == "invoice":
        job.next_action = NextAction(what="Mark paid when the money comes", owner="plumber")
    jobs.log(job, f"{kind}_sent", f"{inv.number} {inr(inv.total)} → " + (", ".join(f"{r['channel']}:{r['status']}" for r in results) or "no channel (shared in app)"))
    jobs.save(job)
    store().put("contacts", f"pendingbill:{user}", {"owner": user, "kind": "done"})
    where = "; ".join(f"{r['channel']} {r.get('delivered_to') or r['to']} — {r['status']}" for r in results) or "no customer channel yet (customer hasn't joined WhatsApp, no email) — PDF is here"
    ops.CARD(user, f"{'🧾 Invoice' if kind == 'invoice' else '📋 Estimate'} {inv.number} · {inr(inv.total)}\n{where}"
             + ("\n[Demo mode: emails go to your own inbox]" if settings().email_demo_override and any(r['channel'] == 'email' for r in results) else ""),
             _card(job, inv, kind, demo, url))
    return True


# ---------------- estimate ----------------
def start_estimate(user: str, job: Job, text: str, image: dict[str, Any] | None, from_customer: bool = False) -> None:
    payload = json.dumps({"request": text, "photo_reading": image, "job_issue": job.issue.value}, ensure_ascii=False)
    d = json_call(settings().openai_model_brain, prompt("estimate_extract"), payload, EstimateDraft, job.job_id, "estimate_extract")
    book, demo = pricebook(user)
    lines = []
    for p in d.likely_parts:
        e = lookup(p.item, book)
        if e:
            lines.append(InvoiceLine(item=e["item"], qty=p.qty, unit_rate=e["rate"], amount=round(p.qty * e["rate"], 2),
                                     source=f"price list{' (demo)' if demo else ''}" + (f" — {p.why}" if p.why else "")))
    lab = lookup("labour", book)
    labour = round(d.labour_hours * (lab["rate"] if lab else 0), 2)
    total = round(sum(x.amount for x in lines) + labour, 2)
    job.estimate = Invoice(lines=lines, labour=labour, total=total, state="awaiting_plumber", date=datetime.now(IST).strftime("%d %b %Y"),
                           idempotency_key=hashlib.sha1(json.dumps([x.model_dump() for x in lines] + [labour]).encode()).hexdigest()[:12])
    jobs.log(job, "estimate_drafted", f"{'Customer photo → ' if from_customer else ''}estimate {inr(total)} · {d.problem} · {d.urgency}")
    jobs.save(job)
    store().put("contacts", f"pendingbill:{user}", {"owner": user, "job_id": job.job_id, "kind": "estimate", "ts": time.time()})
    head = (f"📷 Customer sent a photo for {job.job_id}. " if from_customer else "") + f"Looks like: {d.problem} ({d.urgency})."
    q = ("\nAsk them: " + " / ".join(d.questions[:2])) if d.questions else ""
    ops.CARD(user, head + "\n" + readback(job, job.estimate, [], [], kind="estimate") + q
             + ("\n(Demo price list — set your own rates in settings.)" if demo else ""), _card(job, job.estimate, "estimate", demo))
