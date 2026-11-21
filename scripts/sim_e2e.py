"""SIMULATED-call end-to-end with LIVE OpenAI brain.
WhatsApp text -> route -> confirm -> brief (live) -> [fake Omni] -> transcript -> extract (live) -> rank -> reply (live).
Run: python -m scripts.sim_e2e
"""
import json

from app.engine import runner, steps
from app.store.dynamo import MemoryStore, set_store, store

set_store(MemoryStore("data/sim_store.json"))
dispatched: list = []
steps.DISPATCH = lambda to, ctx: dispatched.append((to, ctx)) or {"requestId": f"sim-{len(dispatched)}"}
say = lambda m: print("\n[CHINKU → WA]\n" + m)

USER = "whatsapp:+910000000000"
msg = ("Chinku, 50 bags cement kavali. Ee dealers ki call chesi best rate kanukko: "
       "Ravi Traders 98480 12345, Sai Cements 9000012345")
print("[USER → WA]", msg)
runner.handle_message(USER, msg, None, say)
runner.handle_message(USER, "yes", None, say)

print("\n--- briefs written by OpenAI ---")
for to, ctx in dispatched:
    print(to[-4:], "::", ctx["call_script"][:600], "\n")

TRANSCRIPTS = {
    "Ravi": ("assistant: Namaste, I am Chinku, an AI assistant calling for Hanish. 50 bags cement rate entha?\n"
             "user: OPC 53 grade aithe bag ki 385 rupees.\nassistant: Delivery eppudu?\n"
             "user: Repu delivery pakka.\nassistant: Transport charges extra unnaya?\n"
             "user: Avunu, transport extra 2000.\nassistant: Payment?\nuser: Cash on delivery."),
    "Sai": ("assistant: Namaste, I am Chinku, an AI assistant calling for Hanish. 50 bags cement rate entha?\n"
            "user: Bag ki moodu vandala tombhai rupayalu.\nassistant: Delivery?\n"
            "user: Repu, kani evening confirm chesta.\nassistant: Transport?\nuser: Transport free andi.\n"
            "assistant: Payment terms?\nuser: Half advance."),
}
for c in sorted(store().scan("calls"), key=lambda c: c["created_at"]):
    key = "Ravi" if "Ravi" in (c["callee"].get("name") or "") else "Sai"
    runner.on_call_result({"call_id": c["omni_call_id"], "transcript": TRANSCRIPTS[key]},
                          lambda u, m: say(m))

print("\n--- extracted ---")
for c in store().scan("calls"):
    print(c["callee"]["name"], json.dumps([(f["name"], f["value"], f["status"], f["evidence_quote"])
                                           for f in c["extracted"]["fields"]], ensure_ascii=False))
t = store().scan("tasks")[0]
print("\nTASK", t["status"], "ranking:", json.dumps(t["steps"]["report"]["output"]["ranking"], ensure_ascii=False))
