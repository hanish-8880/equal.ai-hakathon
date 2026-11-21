"""Pull real role-play call transcripts from Omni into eval/calls/, labelled from the role-play card.
Run after each call:
  python -m eval.collect --card A2 --volunteer v1 [--fresh] [--since "2026-11-21 19:00"]
Takes the newest completed outbound call (after --since, IST) and writes eval/calls/rp_<card>_<volunteer>.json.
"""
import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.config import settings

C = lambda v: {"value": v, "status": "confirmed"}
T = lambda v: {"value": v, "status": "tentative"}
N = {"value": None, "status": "not_mentioned"}
R = {"value": None, "status": "refused"}
CARDS = {  # labels fixed BEFORE the call (from eval/ROLEPLAY.md)
    "A1": {"price_per_unit": C(390), "delivery_time": C("day after tomorrow"), "transport_charge": C(500), "payment_terms": C("cash on delivery")},
    "A2": {"price_per_unit": T("380 to 395"), "delivery_time": T("Thursday"), "transport_charge": N, "payment_terms": N},
    "A3": {"price_per_unit": C(1100), "delivery_time": C("tomorrow"), "transport_charge": C(0), "payment_terms": C("full advance")},
    "A4": {"price_per_unit": R, "delivery_time": C("within 3 days"), "transport_charge": N, "payment_terms": N},
    "A5": {"price_per_unit": C(380), "delivery_time": C("tomorrow"), "transport_charge": C(2000), "payment_terms": C("half advance")},
    "A6": {"price_per_unit": C(400), "delivery_time": T("Monday"), "transport_charge": C(0), "payment_terms": C("30 days credit")},
    "A7": {"price_per_unit": T(1800), "delivery_time": N, "transport_charge": N, "payment_terms": N},
    "A8": {"price_per_unit": C(385), "delivery_time": N, "transport_charge": N, "payment_terms": N},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True, choices=sorted(CARDS))
    ap.add_argument("--volunteer", required=True)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--since", help="IST, e.g. '2026-11-21 19:00'")
    a = ap.parse_args()
    s = settings()
    rows = httpx.get(f"{s.omnidim_base_url}/calls/logs", params={"pageno": 1, "pagesize": 20, "agentid": s.omnidim_agent_id},
                     headers={"Authorization": f"Bearer {s.omnidim_api_key}"}, timeout=30).json()["call_log_data"]
    since_utc = (datetime.strptime(a.since, "%Y-%m-%d %H:%M") - timedelta(hours=5, minutes=30)) if a.since else None
    for x in rows:  # newest first
        t = datetime.strptime(x["time_of_call"], "%m/%d/%Y %H:%M:%S")
        if since_utc and t < since_utc:
            continue
        conv = re.sub(r"<br\s*/?>", "\n", x.get("call_conversation") or "").replace("LLM:", "assistant:").replace("User:", "user:").strip()
        if sum(1 for l in conv.splitlines() if l.startswith("user:")) < 1:
            continue
        cid = f"rp_{a.card}_{a.volunteer}"
        out = {"id": cid, "source": "real_call", "fresh": a.fresh, "spec": "dealer_quotes", "step": "ask_dealers",
               "transcript": conv, "expected": CARDS[a.card], "omni_log_id": x["id"],
               "labelled_by": f"role-play card {a.card} (answers fixed before the call)", "notes": f"volunteer {a.volunteer}"}
        Path(__file__).parent.joinpath("calls", f"{cid}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("saved", cid, "from call", x["id"], x["time_of_call"], "\n", conv[:600])
        return
    print("no matching call found")


if __name__ == "__main__":
    main()
