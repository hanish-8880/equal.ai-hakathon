"""Run EVERY feature end to end with the LIVE OpenAI brain; calls / WhatsApp / email are SIMULATED.
Nobody is called or emailed. Prints PASS/FAIL per feature with the key evidence.
Run: python -m scripts.smoke_all
"""
import json
import time
import traceback
from pathlib import Path

from app.brain import perceive
from app.engine import runner, steps
from app.store.dynamo import MemoryStore, set_store, store

set_store(MemoryStore(None))
DISPATCHED: list = []
SENT: list = []       # (to, text, voice?)
MAILS: list = []
steps.DISPATCH = lambda to, ctx: DISPATCHED.append((to, ctx)) or {"requestId": f"sim-{len(DISPATCHED)}"}
steps.NOTIFY = lambda to, text, audio=None: SENT.append((to, text, bool(audio)))
steps.email_action.send = lambda to, subj, body: MAILS.append((to, subj, body)) or to
steps.QUEUE_POLL_S = 0.01
U = "whatsapp:+919989683607"
MEDIA = Path("media_tmp")
results: list[tuple[str, bool, str]] = []


def say(m: str) -> None:
    SENT.append((U, m, False))


def msg(text: str, image: dict | None = None) -> None:
    runner.handle_message(U, text, image, say)


def finish_call(transcript: str) -> dict:
    [c] = [c for c in store().scan("calls") if c["status"] == "dispatched"]
    runner.on_call_result({"call_id": c["omni_call_id"], "transcript": transcript}, lambda u, m: SENT.append((u, m, False)))
    return store().get("calls", c["call_id"])


def fields(call: dict) -> dict:
    return {f["name"]: (f["value"], f["status"]) for f in (call.get("extracted") or {}).get("fields", [])}


def last_task(spec: str) -> dict:
    return max([t for t in store().scan("tasks") if t["spec"] == spec], key=lambda t: t["created_at"])


def feature(name):
    def wrap(fn):
        t0 = time.time()
        try:
            ok, note = fn()
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"EXCEPTION {e}\n{traceback.format_exc()[-600:]}"
        results.append((name, ok, f"{note}  ({time.time() - t0:.0f}s)"))
        print(f"\n[{'PASS' if ok else 'FAIL'}] {name}: {note}", flush=True)
        return fn
    return wrap


@feature("F1 dealer_quotes (2 dealers, transport flips winner)")
def f1():
    msg("50 bags cement kavali. Ravi Traders 9848012345, Sai Cements 9000012345 ki call chesi best rate kanukko")
    msg("yes")
    T = {"Ravi": "assistant: rate entha?\nuser: Bag ki 385. Transport extra 2000.\nassistant: Delivery?\nuser: Repu pakka.\nassistant: Payment?\nuser: Cash on delivery.",
         "Sai": "assistant: rate entha?\nuser: Bag ki moodu vandala tombhai.\nassistant: Delivery?\nuser: Repu, kani evening confirm chesta.\nassistant: Transport?\nuser: Free.\nassistant: Payment?\nuser: Half advance."}
    for _ in range(2):
        [c] = [c for c in store().scan("calls") if c["status"] == "dispatched"][:1]
        key = "Ravi" if "Ravi" in (c["callee"].get("name") or "") else "Sai"
        runner.on_call_result({"call_id": c["omni_call_id"], "transcript": T[key]}, lambda u, m: SENT.append((u, m, False)))
    t = last_task("dealer_quotes")
    rank = t["steps"]["report"]["output"]["ranking"]
    ok = t["status"] == "done" and rank[0]["callee"].startswith("Sai") and rank[0]["total_price"] == 19500 and rank[1]["total_price"] == 21250
    return ok, f"ranking={[(r['callee'], r['total_price'], r['tentative']) for r in rank]}"


@feature("F2 banner_rate (vision on mock banner → call → listed vs final)")
def f2():
    img = perceive.read_image((MEDIA / "banner_demo.png").read_bytes(), "image/png").model_dump()
    msg("", img)
    msg("ee builder ki call chesi final rate adugu")
    msg("yes")
    call = finish_call("assistant: Final price entha?\nuser: 2BHK final 62 lakhs.\nassistant: Negotiable aa?\nuser: Konchem negotiable.\n"
                       "assistant: Price lo emi include?\nuser: Car parking, club house.\nassistant: Offers?\nuser: Diwali offer free modular kitchen.\n"
                       "assistant: Site visit?\nuser: Sunday 11 AM.")
    t = last_task("banner_rate")
    f = fields(call)
    ok = t["status"] == "done" and t["inputs"]["phone"] == "+919989683607" and f.get("final_price", (0,))[0] == 6200000
    return ok, f"phone={t['inputs']['phone']} listed={t['inputs'].get('listed_price')} final={f.get('final_price')} reply={SENT[-1][1][:90]!r}"


@feature("F3 warranty_email (product photo → web search → draft → yes → send)")
def f3():
    img = perceive.read_image((MEDIA / "product_demo.png").read_bytes(), "image/png").model_dump()
    msg("", img)
    msg("mixer pani cheyatledu, motor on avvatledu. warranty mail pettu")
    t = last_task("warranty_email")
    draft_ok = t["status"] == "awaiting_confirm" and MAILS == []
    msg("yes")
    t = last_task("warranty_email")
    ok = draft_ok and t["status"] == "done" and len(MAILS) == 1 and "@" in MAILS[0][0]
    return ok, f"to={MAILS[0][0] if MAILS else None} subject={MAILS[0][1] if MAILS else None!r}"


@feature("F4 passbook_explain (call → wait photo → explain call → Telugu voice note)")
def f4():
    msg("contact mummy 9989683607")
    msg("Mummy ki passbook ardham kaavatledu, explain chey")
    msg("yes")
    finish_call("assistant: Amma, passbook photo pampandi.\nuser: Sare pamputa.")
    waiting = last_task("passbook_explain")["status"] == "waiting_input"
    img = perceive.read_image((MEDIA / "passbook_demo.png").read_bytes(), "image/png").model_dump()
    runner.handle_message(U, "", img, say)  # mummy's photo (same phone in demo)
    brief = [c for c in store().scan("calls") if c["step_id"] == "explain"][0]["brief"]["call_script"]
    finish_call("assistant: ...\nuser: Ardham ayyindi. LIC enti?\nassistant: ...\nuser: Sare.")
    t = last_task("passbook_explain")
    voice = [s for s in SENT if s[2]]
    ok = waiting and t["status"] == "done" and len(voice) == 1 and ("54" in brief or "yabai nalugu" in brief.lower())
    return ok, f"entries={len(img['fields'].get('entries', []))} voice_notes={len(voice)} brief_has_balance={'yabai nalugu' in brief.lower() or '54' in brief}"


@feature("F5 appointment (clinic → mummy → report → schedule → reminder)")
def f5():
    msg("Repu mummy ki doctor appointment book chey, clinic number 9989683607, 1 hr mundu gurthu chey")
    msg("yes")
    finish_call("assistant: Appointment kavali.\nuser: Repu podduna 11 ki slot undi, Doctor Rao.\nassistant: Fee?\nuser: Aidu vandalu.\nassistant: Correct aa?\nuser: Avunu.")
    finish_call("assistant: Amma, repu 11 ki Dr Rao.\nuser: Sare, time OK.")
    t = last_task("appointment")
    st = t["steps"]["wait_until_reminder"]
    scheduled = st.get("status") == "scheduled"
    runner.fire_schedule(t["task_id"], "wait_until_reminder")
    finish_call("assistant: Amma, gurthu.\nuser: Sare, bayaluderutunna.")
    t = last_task("appointment")
    return scheduled and t["status"] == "done", f"slot={st.get('slot')} real_fire=-60min ok={st.get('real_fire_at') and abs((st['real_fire_at'] + 3600) - __import__('datetime').datetime.fromisoformat(st['slot']).timestamp()) < 1}"


@feature("F6 doc_total_email (PDF → total in code → draft → yes → send)")
def f6():
    MAILS.clear()
    msg("contact suresh suresh@example.com")
    doc = perceive.read_document((MEDIA / "invoice_demo.pdf").read_bytes(), "invoice.pdf").model_dump()
    msg("", doc)
    msg("ee PDF total cheppu, aa total Suresh ki mail pettu")
    t = last_task("doc_total_email")
    tot = t["steps"].get("total", {}).get("output", {})
    msg("yes")
    t = last_task("doc_total_email")
    ok = tot.get("total") == 64965 and tot.get("matches_stated") is True and t["status"] == "done" and MAILS and MAILS[0][0] == "suresh@example.com"
    return ok, f"total={tot.get('total_str')} matches_printed={tot.get('matches_stated')} mail_to={MAILS[0][0] if MAILS else None} body_has_total={bool(MAILS) and '64,965' in MAILS[0][2]}"


@feature("Inbound call → task (owner only)")
def inbound():
    import app.main as m
    got = []
    orig = m.runner.handle_message
    m.runner.handle_message = lambda user, text, image, send: got.append((user, text))
    m.whatsapp.send = lambda to, body, media=None: None
    base = {"direction": "inbound", "summary": "Caller asked for PDF total mailed to Suresh",
            "transcript": "assistant: Chinku here, em cheyyali?\nuser: PDF total Suresh ki mail pettu\nassistant: Correct aa?\nuser: Avunu"}
    owner = m.settings().user_whatsapp
    m.process_omni({**base, "from_number": "+918888888888"})
    stranger_blocked = got == []
    m.process_omni({**base, "from_number": owner.replace("whatsapp:", "")})
    m.runner.handle_message = orig
    return stranger_blocked and len(got) == 1, f"stranger_blocked={stranger_blocked} owner_task_text={got[0][1][:60] if got else None!r}"


print("\n================ SUMMARY ================")
for name, ok, note in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
print(f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
Path("data").mkdir(exist_ok=True)
Path("data/smoke_all.json").write_text(json.dumps([{"feature": n, "pass": ok, "note": note} for n, ok, note in results],
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
