"""END-TO-END run AS THE OWNER on the server, visible in the owner's Chinku App chat.
REAL: brain (OpenAI), DynamoDB, slot locks, maps (OSM), PDF, Gmail (demo override → own inbox), WhatsApp to the customer
(= the owner's own phone, joined to the sandbox).  SIMULATED: what the customer / shop SAY on calls (scripted answers, marked 🧪)
— no phone rings during this run. Run on the server: python -m scripts.e2e_owner"""
import json
import time
import traceback

import app.main as m  # wires CARD / NOTIFY / SEND_FILE hooks exactly like the live server
from app.engine import runner, steps
from app.engine.validate import indian_mobile
from app.jobs import assistant, billing
from app.jobs import store as jobs
from app.store.dynamo import store

s = m.settings()
U = s.user_whatsapp
CUST = indian_mobile(U.replace("whatsapp:", ""))   # the customer in this run is the owner's own (sandbox-joined) phone
SHOP = CUST                                          # the shop too — nobody else is involved
dispatched: list = []
steps.DISPATCH = lambda to, ctx: dispatched.append((to, ctx)) or {"requestId": f"e2e-{int(time.time() * 1000)}"}
report: list[tuple[str, bool, str]] = []


def plumber(text: str) -> None:
    """What the plumber types in the app (logged into his chat like a real message)."""
    m.chat_log(U, "chat_in", text)
    assistant.handle(U, text, None, m.reply_to(U))


def my_task(spec: str) -> dict:
    """THIS run's task for this job (never an older run's task)."""
    return max([t for t in store().scan("tasks", user=U) if t["spec"] == spec and (t.get("inputs") or {}).get("job_id") == state.get("job")],
               key=lambda t: t["created_at"])


def answer_call(spec: str, transcript: str) -> None:
    tid = my_task(spec)["task_id"]
    c = max([c for c in store().scan("calls", status="dispatched") if c["task_id"] == tid], key=lambda c: c["created_at"])
    m.deliver(U, f"🧪 SIMULATED call answer ({c['callee'].get('name') or c['callee'].get('role')}): " + transcript.split("user:")[1].split("\n")[0].strip() + " …")
    runner.on_call_result({"call_id": c["omni_call_id"], "transcript": transcript}, lambda u, t: m.deliver(u, t))


def step(name):
    def wrap(fn):
        try:
            note = fn()
            report.append((name, True, note))
            print(f"✅ {name}: {note}", flush=True)
        except Exception as e:  # noqa: BLE001
            report.append((name, False, f"{e}"))
            print(f"❌ {name}: {e}\n{traceback.format_exc()[-700:]}", flush=True)
        return fn
    return wrap


m.chat_log(U, "chat_out", "🧪 END-TO-END TEST RUN starting — calls are answered by a script (simulated); WhatsApp + email are real.")
m.set_channel(U, "web")
store().put("contacts", f"{U}:srinivas", {"user": U, "alias": "srinivas", "phone": SHOP, "relation": "shop"})
state: dict = {}


@step("1 · Create job by typing")
def _():
    plumber(f"New job. Customer {CUST[3:8]} {CUST[8:]}, kitchen tap leak at Flat 304, Green Park, Kondapur. "
            f"I'm free tomorrow 11 to 1 or after 4. Bill Sai Facility, {s.smtp_user}, under WO-117.")
    j = [j for j in jobs.open_jobs(U) if not j.sample][-1]
    state["job"] = j.job_id
    assert j.roles.site_contact.phone == CUST and len(j.appointment.plumber_available) >= 2
    return f"{j.job_id} · {j.issue.value} · {j.flat.value} · windows {[w.label for w in j.appointment.plumber_available]}"


@step("2 · Booking call → slot locked → WhatsApp confirmation → confirmation call")
def _():
    jid = state["job"]
    plumber(f"{jid} customer ki call chesi visit book chey")
    plumber("yes")
    assert dispatched, "booking call not dispatched"
    offered = my_task("book_visit")["inputs"]["windows"]  # taken slots are never offered
    pick = offered[-1]
    state["offered"] = [w["label"] for w in offered]
    answer_call("book_visit", f"assistant: Which time suits you: {', '.join(w['label'] for w in offered)}?\nuser: {pick['label']} is fine. "
                              f"Flat 304, third floor, watchman will open the gate.\nassistant: {pick['label']}, Flat 304 — correct?\nuser: Yes, correct.")
    j = jobs.get(jid)
    assert j.appointment.state == "booked", j.appointment.state
    wa = [e for e in store().events("-") if e["kind"] == "wa_out_customer" and e["ts"] > time.time() - 120]
    # fire the confirmation-call schedule now (simulated time) so the live server never rings the phone for this test
    runner.fire_schedule(my_task("book_visit")["task_id"], "wait_confirm")
    answer_call("book_visit", "assistant: Is tomorrow 4 to 7 still fine, and who will open?\nuser: Yes fine, watchman will open.\nassistant: Thank you.")
    j = jobs.get(jid)
    return (f"offered {state['offered']} (taken slots excluded) → booked {j.appointment.slot.label}; WhatsApp to customer: "
            f"{'sent' if wa else 'NOT sent'}; confirm call done → next: {j.next_action.what}")


@step("3 · Parts: nearby-shops map card + shop call + price comparison + ₹500 rule")
def _():
    jid = state["job"]
    plumber(f"{jid} ki rendu Jaquar angle valves ALD-0517 half inch kavali. Srinivas ki call chesi price kanukko. "
            f"Srinivas daggara 500 lopu exact model unte customer ni 4 to 6 adugu.")
    plumber("yes")
    answer_call("part_sourcing", "assistant: Do you have Jaquar ALD-0517 half inch, 2 pieces, today?\nuser: Exact one comes tomorrow. Today I have ALD-0520.\n"
                                 "assistant: Price per piece?\nuser: 280 for the 0520, MRP 350.\nassistant: Pickup?\nuser: Kondapur main road shop, from 10 am.")
    for _ in range(45):  # the map card loads in the background (OSM ~30 s)
        if any((e["payload"].get("card") or {}).get("type") == "shops" for e in store().events(f"chat:{U}")[-15:]):
            break
        time.sleep(2)
    shops = [e for e in store().events(f"chat:{U}")[-20:] if (e["payload"].get("card") or {}).get("type") == "shops"]
    j = jobs.get(jid)
    dep = j.dependencies[-1] if j.dependencies else None
    return (f"map card: {len(shops[-1]['payload']['card']['rows']) if shops else 0} shops; offers {len(j.shop_offers)}; "
            f"rule {dep.state if dep else '—'} ({dep.reason if dep else ''})")


@step("4 · Customer photo → estimate → send (WhatsApp)")
def _():
    from app.brain.perceive import read_image
    jid = state["job"]
    img = read_image(open("media_tmp/leak_demo.png", "rb").read(), "image/png").model_dump() if __import__("os").path.exists("media_tmp/leak_demo.png") else None
    assert img, "media_tmp/leak_demo.png missing"
    billing.start_estimate(U, jobs.get(jid), "customer photo of the leak", img, from_customer=True)
    plumber("yes")
    j = jobs.get(jid)
    return f"estimate {j.estimate.number} ₹{j.estimate.total:,.0f} · {j.estimate.state}"


@step("5 · Closing note → invoice ₹1,400 → PDF → Gmail + WhatsApp")
def _():
    jid = state["job"]
    plumber(f"{jid} finished. Fitted two angle valves at 350 each and one hose at 300. Labour 400. The third valve was unused and returned. "
            f"Send the bill to the company.")
    j = jobs.get(jid)
    assert j.invoice.total == 1400, j.invoice.total
    plumber("yes")
    j = jobs.get(jid)
    res = j.invoice.send_attempts[-1]["results"] if j.invoice.send_attempts else []
    return f"{j.invoice.number} ₹{j.invoice.total:,.0f} · {j.invoice.state} · " + ", ".join(f"{r['channel']}:{r['status']}" for r in res)


@step("6 · Inbound call from the plumber's number → confirmation shows the caller")
def _():
    m.process_inbound_call({"from_number": CUST, "direction": "inbound", "summary": "Plumber asked for the status of his job.",
                            "transcript": f"assistant: Hello! Chinku here.\nuser: {state['job']} status enti?\nassistant: Okay."})
    last = [e["payload"]["text"] for e in store().events(f"chat:{U}")[-4:]]
    return " | ".join(t[:70] for t in last)


m.chat_log(U, "chat_out", "🧪 END-TO-END TEST RUN finished: " + ", ".join(f"{'✅' if ok else '❌'} {n.split(' · ')[0]}" for n, ok, _ in report))
print(json.dumps([{"step": n, "ok": ok, "note": note} for n, ok, note in report], ensure_ascii=False, indent=1))
