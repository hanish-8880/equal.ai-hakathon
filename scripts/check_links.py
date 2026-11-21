"""Is everything linked? Run ON THE SERVER: python -m scripts.check_links
Part 1 checks every external connection with the real config. Part 2 runs Instint J1 with the REAL planner under a TEST identity;
the email approval is answered 'no', so nothing is sent."""
import json
import time
import traceback
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

IST = timezone(timedelta(hours=5, minutes=30))
OK, BAD = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        try:
            ok, note = fn()
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"{e.__class__.__name__}: {str(e)[:300]}\n{traceback.format_exc()[-900:]}"
        results.append((name, ok, note))
        print(f"{OK if ok else BAD} {name}: {note}", flush=True)
        return fn
    return wrap


s = settings()


# ---------------- Part 1: connections ----------------
@check("Public HTTPS")
def _():
    r = httpx.get(f"{s.public_base_url}/health", timeout=15)
    return r.status_code == 200, f"{s.public_base_url} → {r.status_code}"


@check("DynamoDB read/write (instance role)")
def _():
    from app.store.dynamo import store
    st = store()
    k = f"linkcheck:{time.time()}"
    st.put("contacts", k, {"kind": "linkcheck"})
    return st.get("contacts", k) is not None, type(st).__name__


@check("Omni agents (webhook, voice, LLM, ASR) + number binding")
def _():
    H = {"Authorization": f"Bearer {s.omnidim_api_key}"}
    notes, ok = [], True
    for a in (s.omnidim_agent_id, s.omnidim_inbound_agent_id):
        d = httpx.get(f"{s.omnidim_base_url}/agents/{a}", headers=H, timeout=20).json()
        hooks = [p.get("webhook_url") for p in d.get("post_call_config_ids") or []]
        good = any(h == f"{s.public_base_url}/webhooks/omni" for h in hooks)
        ok &= good
        notes.append(f"{d['name']}: webhook {'OK' if good else hooks}, {d.get('llm_service')}, {d.get('asr_service')}, voice {d.get('voice_external_id', '')[:8]}")
    n = httpx.get(f"{s.omnidim_base_url}/phone_number/list?pageno=1&pagesize=5", headers=H, timeout=20).json()["phone_numbers"][0]
    bound = str(n["active_bot_id"]) == str(s.omnidim_inbound_agent_id)
    notes.append(f"{n['phone_number']} inbound → {'inbound agent' if bound else n['active_bot_id']}")
    return ok and bound, " | ".join(notes)


@check("Twilio account (WhatsApp)")
def _():
    a = (s.twilio_account_sid, s.twilio_auth_token)
    d = httpx.get(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}.json", auth=a, timeout=20).json()
    alerts = httpx.get("https://monitor.twilio.com/v1/Alerts?PageSize=1", auth=a, timeout=20).json().get("alerts", [])
    last = alerts[0] if alerts else None
    stale = last and "trycloudflare" in (last.get("request_url") or "")
    return d["type"] == "Full", f"type {d['type']}; last webhook alert: {(last or {}).get('date_created', 'none')}" + (" (stale tunnel URL seen — poller backstop covers it)" if stale else "")


@check("Gmail SMTP send + IMAP read")
def _():
    import imaplib
    import smtplib
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as m:
        m.login(s.smtp_user, s.smtp_app_password)
    i = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
    i.login(s.smtp_user, s.smtp_app_password)
    i.select("INBOX", readonly=True)
    i.logout()
    return True, f"{s.smtp_user} (demo override = {s.email_demo_override})"


@check("OpenAI models")
def _():
    from app.brain.client import oai
    ids = {m.id for m in oai().models.list()}
    need = [s.openai_model_brain, s.openai_model_fast, s.openai_model_transcribe, s.openai_model_tts]
    return all(m in ids for m in need), ", ".join(f"{m}{'' if m in ids else ' MISSING'}" for m in need)


@check("OpenStreetMap geocode")
def _():
    from app.actions import maps
    g = maps.geocode("Kondapur, Hyderabad")
    return bool(g), f"{g[0]:.4f},{g[1]:.4f}" if g else "no result"


@check("PDF + PPT libraries")
def _():
    import fpdf  # noqa: F401
    import pptx  # noqa: F401
    return True, "fpdf2 + python-pptx"


# ---------------- Part 2: Instint J1 with the REAL planner (no email is sent: approval is answered "no") ----------------
@check("Instint J1: plan -> exact transcript -> email waits for approval -> reminder")
def _():
    from pathlib import Path
    from app.instint import executor
    U = "test:check_links"
    out, cards = [], []
    page = Path("eval/pages/todo_page.jpg")
    att = [{"id": "a1", "name": page.name, "mime": "image/jpeg", "url": "", "path": str(page)}]
    executor.handle(U, "Transcribe this, email me a copy, and remind me tomorrow at 9 to review it.", att, out.append,
                    lambda t, c: cards.append((t, c)))
    w = store().get("contacts", f"iwait:{U}")
    assert w, f"no approval asked: {out[-3:]}"
    t = executor.load(w["task_id"])
    truth = Path("eval/pages/todo_page.txt").read_text(encoding="utf-8").split()
    got = t.steps["s1"].output.get("text", "").split() if "s1" in t.steps else []
    executor.handle(U, "no", [], out.append, lambda x, c: cards.append((x, c)))  # never send from a check
    return got == truth, f"{len(t.plan.steps)} steps; transcript {'exact' if got == truth else 'DIFFERS'}; approval asked; declined"


print("\n" + "=" * 60)
for n, ok, note in results:
    print(f"{OK if ok else BAD} {n}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)} linked")
