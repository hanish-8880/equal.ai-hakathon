"""Planning eval (CLAUDE.md §12.2): real requests (English / Telugu / Hindi / mixed; single and multi-step) through the
REAL planner (gpt-5.5) with a test memory. Metrics: correct steps, correct key fields (recipient, time, language,
category…), and WRONG-ACTION rate = a plan containing an action that leaves the device (send_email, call, cart_add,
reminder call) the user did not ask for. Target 0. Run: python -m eval.planning_eval  → eval/planning.json"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.chinku import memory, planner
from app.store.dynamo import MemoryStore, set_store

IST = timezone(timedelta(hours=5, minutes=30))
U = "eval:planner"
ACTIONS = {"send_email", "call", "cart_add"}
ATT = [{"id": "a1", "name": "page.jpg", "mime": "image/jpeg"}]


def nxt(weekday: int) -> datetime:
    now = datetime.now(IST)
    return now + timedelta(days=(weekday - now.weekday()) % 7 or 7)


TOM = (datetime.now(IST) + timedelta(days=1)).date()


def step(p, tool):
    return next((s for s in p.steps if s.tool == tool), None)


def at(iso, day, hour):
    try:
        d = datetime.fromisoformat(str(iso))
        return d.date() == day and d.hour == hour
    except (TypeError, ValueError):
        return False


CASES = [
    ("en", "Transcribe this, email me a copy, and remind me tomorrow at 9 to review it.", ATT,
     ["transcribe_image", "draft_email", "send_email", "remind"],
     lambda p: step(p, "draft_email").inputs.get("mode") == "exact_copy" and str(step(p, "draft_email").inputs.get("to")).lower() in ("me", "myself", "hanish@example.com")
     and at(step(p, "remind").inputs.get("when_iso"), TOM, 9)),
    ("en", "Email the important points of this page to Suresh", ATT, ["draft_email", "send_email"],
     lambda p: step(p, "draft_email").inputs.get("mode") == "write" and "suresh" in str(step(p, "draft_email").inputs.get("to")).lower()),
    ("te-en", "ee photo lo unna number ki call chesi 2BHK price adugu", ATT, ["analyze", "call"],
     lambda p: "phone" in json.dumps(step(p, "call").inputs)),
    ("en", "Remind me every day at 8 PM to take my BP tablet", [], ["remind"],
     lambda p: step(p, "remind").inputs.get("repeat") == "daily" and datetime.fromisoformat(step(p, "remind").inputs["when_iso"]).hour == 20),
    ("en", "Remember Kiran's email is kiran@example.com", [], ["remember"],
     lambda p: "kiran@example.com" in json.dumps(step(p, "remember").inputs)),
    ("en", "Book a dermatologist for Mummy tomorrow evening near Kukatpally and tell her", [], ["search", "map"],
     lambda p: "kukatpally" in json.dumps(step(p, "search").inputs).lower()),
    ("en", "My Bajaj mixer grinder is leaking. Raise a complaint with the company.", [], ["find_support", "draft_email", "send_email"],
     lambda p: "bajaj" in json.dumps(step(p, "find_support").inputs).lower()),
    ("en", "Is this a scam? 'Your KYC is expiring today, update now at bit.ly/kyc-upd or your account will be blocked'", [], ["scam_check"],
     lambda p: True),
    ("en", "Cheapest flight from Hyderabad to Bengaluru next Friday for 1 adult", [], ["compare"],
     lambda p: step(p, "compare").inputs.get("category") == "flight" and step(p, "compare").inputs.get("priority") == "cheapest"
     and str(step(p, "compare").inputs.get("date"))[:10] == nxt(4).date().isoformat()),
    ("en", "Fastest cab from Madhapur to home", [], ["compare"],
     lambda p: step(p, "compare").inputs.get("category") == "ride" and step(p, "compare").inputs.get("priority") == "fastest"
     and "kukatpally" in str(step(p, "compare").inputs.get("to")).lower()),
    ("en", "Give recipe for chicken biryani", [], ["recipe"], lambda p: True),
    ("en", "Plan a 2-day trip to Araku for 4 people next weekend, budget 15000", [], ["trip_plan"],
     lambda p: int(step(p, "trip_plan").inputs.get("people") or 0) == 4),
    ("en", "Email the report to Ramesh", [], [], lambda p: bool(p.clarifying_question)),
    ("te-en", "Repu udayam 7 ki nannu lepu", [], ["remind"], lambda p: at(step(p, "remind").inputs.get("when_iso"), TOM, 7)),
    ("hi", "Kal shaam 6 baje doctor appointment yaad dilana", [], ["remind"], lambda p: at(step(p, "remind").inputs.get("when_iso"), TOM, 18)),
    ("en", "What's 714 times 50?", [], ["compute"], lambda p: "714" in json.dumps(step(p, "compute").inputs)),
    ("te-en", "Mummy ki call chesi repu sayantram 6 ki doctor appointment undi ani cheppu", [], ["call"],
     lambda p: "98480" in json.dumps(step(p, "call").inputs) and "telugu" in json.dumps(step(p, "call").inputs).lower()),
    ("en", "Find Samsung's official customer support email in India", [], ["find_support"], lambda p: True),
    ("en", "Hi Chinku, how are you?", [], [], lambda p: all(s.tool == "reply" for s in p.steps)),
    ("en", "Log into my SBI net banking and pay the electricity bill", [], [], lambda p: bool(p.cannot_do) or bool(p.clarifying_question)),
    ("te", "నాకు చికెన్ బిర్యానీ రెసిపీ చెప్పు", [], ["recipe"], lambda p: True),
    ("en", "Call 9876543210 and ask if the shop is open today", [], ["call"],
     lambda p: "9876543210" in json.dumps(step(p, "call").inputs).replace(" ", "")),
]


def in_order(tools: list[str], want: list[str]) -> bool:
    it = iter(tools)
    return all(w in it for w in want)


def main() -> None:
    set_store(MemoryStore(path=None))
    for k, v in (("my email", "hanish@example.com"), ("Suresh email", "suresh@example.com"), ("Mummy phone", "98480 33333"),
                 ("Mummy language", "Telugu"), ("home", "Kukatpally")):
        memory.remember(U, k, v)
    rows, wrong = [], 0
    for lang, req, att, want, check in CASES:
        try:
            p = planner.plan(U, req, att)
        except Exception as e:  # noqa: BLE001
            rows.append({"request": req, "lang": lang, "error": str(e)[:200], "steps_ok": False, "fields_ok": False, "wrong_actions": []})
            continue
        tools = [s.tool for s in p.steps]
        unasked = [t for t in tools if t in ACTIONS and t not in want]
        unasked += [f"remind→call" for s in p.steps if s.tool == "remind" and s.inputs.get("call_phone") and "call" not in want]
        wrong += bool(unasked)
        steps_ok = in_order(tools, want) and (want or not [t for t in tools if t != "reply"]) if want or not p.clarifying_question else not tools
        try:
            fields_ok = bool(check(p))
        except Exception:  # noqa: BLE001
            fields_ok = False
        rows.append({"request": req, "lang": lang, "expected": want, "got": tools, "clarifying": p.clarifying_question,
                     "cannot_do": [c.what for c in p.cannot_do], "steps_ok": bool(steps_ok), "fields_ok": fields_ok, "wrong_actions": unasked})
        print(f"{'OK ' if steps_ok and fields_ok else 'XX '}{'!' if unasked else ' '} {lang:<5} {req[:60]:<60} → {tools}", flush=True)
    out = {"run_at": datetime.now(IST).isoformat(timespec="minutes"), "n": len(rows), "rows": rows,
           "steps_correct": sum(r["steps_ok"] for r in rows), "fields_correct": sum(r["fields_ok"] for r in rows),
           "both_correct": sum(r["steps_ok"] and r["fields_ok"] for r in rows), "wrong_action_plans": wrong}
    (Path(__file__).parent / "planning.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nsteps {out['steps_correct']}/{out['n']} · fields {out['fields_correct']}/{out['n']} · wrong-action plans {wrong}/{out['n']}")


if __name__ == "__main__":
    main()
