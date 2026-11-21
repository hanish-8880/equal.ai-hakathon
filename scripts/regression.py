"""LIVE regression of every Chinku feature (run ON THE SERVER: python -m scripts.regression [names…]).
Real planner + real tools (OpenAI, web search, Kiwi, Swiggy, OpenStreetMap, DynamoDB) under a TEST identity.
Anything that would leave the app (call, booking, cart change) is answered "no" at the approval step — except ONE email,
which goes to the owner's own inbox (demo override) to prove sending works. Prints a table + writes eval/regression.json."""
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import prefs
from app.chinku import executor, memory, tools
from app.config import settings
from app.store.dynamo import store

IST = timezone(timedelta(hours=5, minutes=30))
U = "test:regression"
ROOT = Path(__file__).resolve().parents[1]
OUT: list[str] = []
CARDS: list[tuple[str, dict]] = []
results: list[dict] = []


def send(t: str) -> None:
    OUT.append(t)


def card(t: str, c: dict | None) -> None:
    OUT.append(t)
    if c:
        CARDS.append((t, c))


def att(name: str) -> list[dict]:
    p = ROOT / "eval" / "pages" / name
    return [{"id": "a1", "name": p.name, "mime": "image/jpeg", "url": "", "path": str(p)}]


def run(text: str, attachments: list | None = None) -> tuple[list[str], list[dict]]:
    n_out, n_cards = len(OUT), len(CARDS)
    store().add_event(f"chat:{U}", "chat_in", {"text": text})  # same chat log the app keeps → real follow-ups
    executor.handle(U, text, attachments or [], send, card)
    for o in OUT[n_out:]:
        store().add_event(f"chat:{U}", "chat_out", {"text": o})
    return OUT[n_out:], [c for _, c in CARDS[n_cards:]]


def answer(yes: bool) -> tuple[list[str], list[dict]]:
    w = store().get("contacts", f"iwait:{U}")
    assert w, "no approval was waiting"
    n_out, n_cards = len(OUT), len(CARDS)
    executor.approve(U, w["task_id"], w["step_id"], yes, send, card)
    return OUT[n_out:], [c for _, c in CARDS[n_cards:]]


def types(cards: list[dict]) -> list[str]:
    return [c.get("type") for c in cards]


CASES = {}


def case(name: str):
    def wrap(fn):
        CASES[name] = fn
        return fn
    return wrap


@case("J1 photo → exact transcript → email (sent to own inbox) → reminder")
def _():
    out, cards = run("Transcribe this, email me a copy, and remind me tomorrow at 9 to review it.", att("todo_page.jpg"))
    tr = next(c for c in cards if c.get("type") == "transcript")
    truth = (ROOT / "eval/pages/todo_page.txt").read_text(encoding="utf-8").split()
    assert tr["text"].split() == truth, "transcript differs from ground truth"
    assert any(c.get("type") == "email" and c.get("state") == "waiting" and c.get("task_id") for c in cards), "no email approval card"
    out2, cards2 = answer(True)
    assert any(c.get("type") == "email" and c.get("state") == "sent" for c in cards2), f"email not sent: {out2[-2:]}"
    assert any(c.get("type") == "reminder" for c in cards2), "no reminder"
    return "transcript exact · email sent · reminder set"


@case("Memory: remember + secret refused")
def _():
    run("Remember Suresh's email is suresh.test@example.com")
    out, _ = run("Remember my bank OTP is 482913")
    assert memory.get(U, "Suresh email") == "suresh.test@example.com", "Suresh email not saved"
    assert not any("482913" in str(r["value"]) for r in memory.recall(U)), "OTP was stored!"
    return "saved Suresh email · refused OTP"


@case("Follow-up uses memory: email the page to Suresh (approval → no)")
def _():
    out, cards = run("Email the important points of that page to Suresh")
    em = next((c for c in cards if c.get("type") == "email"), None)
    assert em and em["to"] == "suresh.test@example.com" and em.get("task_id"), f"no email approval to Suresh: {out[-2:]}"
    answer(False)
    return "drafted to suresh.test@example.com · declined"


@case("J7 scam check")
def _():
    out, cards = run("Is this a scam? 'Dear customer your SBI KYC expires today. Update now at bit.ly/sbi-kyc or your account will be blocked. Share OTP to verify'")
    sc = next(c for c in cards if c.get("type") == "scam")
    assert sc["verdict"] in ("Likely scam", "Unsure") and len(sc["red_flags"]) >= 2, sc
    return f"{sc['verdict']} · {len(sc['red_flags'])} red flags"


@case("J3 complaint: official support → email approval (no)")
def _():
    out, cards = run("My Bajaj mixer grinder jar is leaking. Raise a complaint with the company by email.")
    sup = next(c for c in cards if c.get("type") == "support")
    assert sup["official"] and sup.get("email"), sup
    em = next((c for c in cards if c.get("type") == "email"), None)
    assert em and em.get("task_id"), f"no complaint email card: {out[-2:]}"
    answer(False)
    return f"official {sup['email']} ({sup['source_url'][:40]}) · complaint drafted · declined"


@case("J3 by text: fan model → number + email → 'mail them' → 'call them' (both → no)")
def _():
    out, cards = run("Crompton ceiling fan, model HS Plus 1200 mm, bought 8 months ago. It makes a loud noise and the "
                     "speed is very slow. Find their support number and email.")
    sup = next(c for c in cards if c.get("type") == "support")
    assert sup.get("email") and sup.get("phone"), sup
    out, cards = run("mail them")
    em = next(c for c in cards if c.get("type") == "email")
    assert em.get("to") and "@" in em["to"], em
    answer(False)
    out, cards = run("call them")
    cp = next(c for c in cards if c.get("type") == "call_plan")
    answer(False)
    return f"{sup['email']} · {sup['phone']} · email to {em['to']} · call card {(cp.get('targets') or [{}])[0].get('phone')} · both declined"


@case("Toll-free call: approval card (night rule allows 1800) → no")
def _():
    out, cards = run("Call 1800 102 5551 and ask about the status of my mixer complaint")
    cp = next((c for c in cards if c.get("type") == "call_plan"), None)
    assert cp and cp["targets"] and cp.get("task_id"), f"no call approval: {out[-3:]}"
    answer(False)
    return f"call card for {cp['targets'][0].get('phone')} · declined"


@case("J4 photo → call (visiting card, own number) → no")
def _():
    out, cards = run("Call this number and ask if a 2BHK is available and the price", att("visiting_card_test.jpg"))
    cp = next((c for c in cards if c.get("type") == "call_plan"), None)
    assert cp and "99896" in json.dumps(cp["targets"]).replace(" ", "")[:400] or cp and "9989683607" in json.dumps(cp["targets"]), f"{out[-3:]}"
    answer(False)
    return f"read {cp['targets'][0].get('name')} · {cp['targets'][0].get('phone')} · declined"


@case("J9 flights (Kiwi LIVE)")
def _():
    out, cards = run("Cheapest flight Hyderabad to Bengaluru next Friday for 1 adult")
    c = next(c for c in cards if c.get("type") == "compare")
    w = c["winner"]
    assert c["status"] == ["LIVE"] and w["final_payable"] and w["link"], c
    return f"{c['total_found']} flights · best ₹{w['final_payable']:,.0f} {w['item'][:40]}"


@case("J9 rides (DEMO)")
def _():
    memory.remember(U, "home", "Kukatpally")
    out, cards = run("Cheapest auto home from Madhapur")
    c = next(c for c in cards if c.get("type") == "compare")
    assert c["status"] == ["DEMO"], c
    return f"{c['winner']['item']} ₹{c['winner']['final_payable']:,.0f}"


@case("Swiggy Food search (LIVE)")
def _():
    out, cards = run("Veg lunch under 300 rupees")
    c = next((c for c in cards if c.get("type") == "food"), None)
    if not c:
        assert any("Nothing on Swiggy fits" in o for o in out), out[-2:]
        return "no dish fits right now (clear message) — kitchens closed at night"
    return f"{len(c['rows'])} dishes · best {c['rows'][0]['dish'][:30]} ₹{c['rows'][0]['price']}"


@case("Swiggy Dineout search → slot check → FREE booking approval (no)")
def _():
    out, cards = run("Book a table for 2 at a biryani place near Kukatpally")
    d = next(c for c in cards if c.get("type") == "dineout")
    r0 = d["rows"][0]
    day = (datetime.now(IST) + timedelta(days=1)).strftime("%A")
    out2, cards2 = run(f"Book a table at {r0['name']} for 2 guests tomorrow 8 pm (restaurant_id {r0['id']}, lat {d['lat']}, lon {d['lon']})")
    ap = next((c for c in cards2 if c.get("type") == "approve_table"), None)
    assert ap and ap["free"] and ap.get("task_id"), f"no table approval: {out2[-3:]}"
    answer(False)
    return f"{len(d['rows'])} places · {r0['name']} {ap['when']} FREE · declined"


@case("Recipe → Instamart cart plan → approval (no)")
def _():
    out, cards = run("Give recipe for tomato rasam and add the ingredients to my cart")
    assert any(c.get("type") == "recipe" for c in cards), "no recipe"
    cp = next((c for c in cards if c.get("type") == "cart_plan" and c.get("state") == "waiting"), None)
    assert cp and cp.get("task_id"), f"no cart approval: {out[-3:]}"
    answer(False)
    return f"{len(cp['rows'])} items · ₹{cp['total']}"


@case("J11 trip planner")
def _():
    out, cards = run("Plan a 2-day trip to Araku for 4 people next weekend")
    t = next(c for c in cards if c.get("type") == "trip")
    assert t["itinerary"] and (t["hotels"] or t["restaurants"]), t
    return f"{len(t['itinerary'])} days · {len(t['hotels'])} hotels · {len(t['restaurants'])} restaurants"


@case("J2 doctor: search + map")
def _():
    out, cards = run("Find a dermatologist near Kukatpally for tomorrow evening")
    r = next(c for c in cards if c.get("type") == "research")
    m = next((c for c in cards if c.get("type") == "map"), None)
    assert r["items"], r
    return f"{len(r['items'])} clinics · map {'yes' if m else 'no'} ({sum(1 for x in (m or {}).get('rows', []) if x['lat'])} pins)"


@case("Reminder call to Mummy needs approval (no)")
def _():
    memory.remember(U, "Mummy phone", "9989683607")
    out, cards = run("Remind Mummy by phone call tomorrow at 6 pm to take her BP tablet, in Telugu")
    cp = next((c for c in cards if c.get("type") == "call_plan"), None)
    assert cp and cp.get("task_id"), f"no approval for reminder call: {out[-3:]}"
    answer(False)
    return "approval card shown · declined"


@case("Compute in code")
def _():
    out, cards = run("What is 714 times 50?")
    assert any("35,700" in o or "35700" in o for o in out), out[-2:]
    return "35,700"


@case("Bare yes with nothing waiting")
def _():
    out, _ = run("yes")
    assert out and out[-1].startswith("Nothing is waiting"), out
    return "handled"


@case("Languages: Telugu / Hindi / mix keep numbers exact")
def _():
    from app.brain import translate
    src = "Recommended: Veg Thali · ₹180 · 40-45 min. Add to your Swiggy cart? (yes / no)"
    res = {c: translate.to_lang(src, c) for c in ("te", "hi", "te-en")}
    for c, t in res.items():
        assert translate.safe(src, t), f"{c}: numbers changed"
    assert any("ఀ" <= ch <= "౿" for ch in res["te"]) and any("ऀ" <= ch <= "ॿ" for ch in res["hi"])
    return " | ".join(f"{c}: {t[:40]}" for c, t in res.items())


def main() -> None:
    prefs.set_language(U, "en")
    memory.remember(U, "my email", settings().smtp_user)
    only = sys.argv[1:]
    for name, fn in CASES.items():
        if only and not any(o.lower() in name.lower() for o in only):
            continue
        store().delete("contacts", f"iwait:{U}")
        t0 = time.time()
        try:
            note = fn()
            ok = True
        except Exception as e:  # noqa: BLE001
            note, ok = f"{type(e).__name__}: {str(e)[:300]}", False
            traceback.print_exc(limit=2)
        results.append({"case": name, "ok": ok, "note": note, "seconds": round(time.time() - t0, 1)})
        print(f"{'PASS' if ok else 'FAIL'}  {round(time.time() - t0):>3}s  {name}  —  {note}", flush=True)
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    (ROOT / "eval" / "regression.json").write_text(json.dumps({"run_at": datetime.now(IST).isoformat(timespec="minutes"), "passed": passed,
                                                               "total": len(results), "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
