"""Chinku extractor vs a plain baseline prompt, on labelled call transcripts in eval/calls/*.json.
Writes eval/results.md and eval/results.json. NEVER hand-edit those files.

Run: python -m eval.run_eval

Metrics (counts, not just %):
- field accuracy: value right (or correctly empty when the person never said / refused)
- wrong-confirmed: a field the person did NOT firmly state (tentative / not mentioned / refused) reported as a firm fact.
  Chinku: status "confirmed". Baseline: it has no status, so any value it reports is presented as fact.
- status accuracy (Chinku only): confirmed / tentative / refused / not_mentioned matches the label.
String values are compared by a small LLM judge (logged in results.json); numbers, booleans and lists in code.
"""
import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.brain.client import json_call
from app.brain.extractor import extract
from app.config import settings
from app.engine.specs_loader import load_specs
from app.store.dynamo import MemoryStore, set_store

set_store(MemoryStore(None))
ROOT = Path(__file__).parent
BASELINE_PROMPT = "Summarise this call and list the price and details."


class Baseline(BaseModel):
    summary: str
    details: dict[str, Any]


class Same(BaseModel):
    same: bool
    why: str


def baseline(transcript: str, fields: list[str]) -> dict[str, Any]:
    user = json.dumps({"transcript": transcript, "fields": fields}, ensure_ascii=False)
    sys = (BASELINE_PROMPT + " Return JSON {\"summary\": str, \"details\": {<field>: value or null}} using exactly the given field names.")
    return json_call(settings().openai_model_brain, sys, user, Baseline, None, "eval_baseline").details


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().replace(",", "")
    if re.search(r"\b(free|no|none|nil|zero|nothing)\b", s) and not re.search(r"\d", s):
        return 0.0  # "free", "no transport charge" = 0
    m = re.search(r"-?\d+(\.\d+)?", s)
    if not m:
        return None
    x = float(m.group(0))
    if "lakh" in s:
        x *= 100000
    if "crore" in s:
        x *= 10000000
    return x


JUDGE_LOG: list[dict[str, Any]] = []
HEDGES = re.compile(r"\b(maybe|roughly|around|approx|approximately|about|tentative|to be confirmed|will confirm|tbc|depends|not sure|probably)\b", re.I)


def same_value(field: str, expected: Any, got: Any) -> bool:
    if got in (None, "", [], "null", "None"):
        return False
    if isinstance(expected, bool):
        txt = str(got).strip().lower()
        g = got if isinstance(got, bool) else (txt in ("true", "1", "y") or bool(re.match(r"(yes|yeah|true)\b", txt)))
        return g == expected
    if isinstance(expected, (int, float)):
        g = _num(got)
        return g is not None and abs(g - expected) <= max(0.5, abs(expected) * 0.005)
    if isinstance(expected, list):
        g = got if isinstance(got, list) else re.split(r"[,;/]| and ", str(got))
        gs = " | ".join(str(x).lower() for x in g)
        hit = sum(1 for e in expected if e.lower() in gs)
        return hit >= max(1, len(expected) // 2)
    e, g = str(expected).lower().strip(), str(got).lower().strip()
    if e in g or g in e:
        return True
    j = json_call(settings().openai_model_brain,
                  "Do these two values mean the same thing for the given field? Be strict about numbers, days and dates; lenient about wording.",
                  json.dumps({"field": field, "expected": expected, "got": got}, ensure_ascii=False), Same, None, "eval_judge")
    JUDGE_LOG.append({"field": field, "expected": expected, "got": got, "same": j.same, "why": j.why})
    return j.same


def score(expected: dict[str, Any], got: dict[str, Any], with_status: bool) -> dict[str, Any]:
    rows = []
    for f, exp in expected.items():
        if with_status:
            g = got.get(f) or {}
            val, st = g.get("value"), g.get("status")
        else:  # baseline: no status field; hedge words in its text count as tentative (fair to the baseline)
            val = got.get(f)
            if isinstance(val, dict) and not val:
                val = None
            st = "not_mentioned" if val in (None, "", [], "null", "None") else ("tentative" if HEDGES.search(str(val)) else "confirmed")
        if exp["status"] in ("confirmed", "tentative"):
            ok = same_value(f, exp["value"], val)
        else:
            ok = val in (None, "", [], "null", "None", 0) if exp["status"] == "not_mentioned" else val in (None, "", [], "null", "None")
        wrong_conf = exp["status"] != "confirmed" and st == "confirmed" and val not in (None, "", [])
        rows.append({"field": f, "expected": exp, "got_value": val, "got_status": st if with_status else None,
                     "correct": ok, "wrong_confirmed": wrong_conf, "status_ok": (st == exp["status"]) if with_status else None})
    return {"fields": rows}


def main() -> None:
    specs = load_specs()
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "calls").glob("*.json")) if not p.name.startswith("_")]
    out = []
    t0 = time.time()
    for c in cases:
        spec = specs[c["spec"]]
        fields = list(c["expected"])
        schema = {f: spec["result_schema"].get(f, {"type": "string"}) for f in fields}
        ch = extract(c["transcript"], fields, schema).model_dump()
        ch_fields = {f["name"]: f for f in ch["fields"]}
        bl = baseline(c["transcript"], fields)
        out.append({"id": c["id"], "source": c["source"], "fresh": c.get("fresh", False), "notes": c.get("notes", ""),
                    "labelled_by": c["labelled_by"],
                    "chinku": score(c["expected"], ch_fields, True), "baseline": score(c["expected"], bl, False),
                    "chinku_raw": ch, "baseline_raw": bl})
        print(f"{c['id']:32} chinku {sum(r['correct'] for r in out[-1]['chinku']['fields'])}/{len(fields)}  "
              f"baseline {sum(r['correct'] for r in out[-1]['baseline']['fields'])}/{len(fields)}", flush=True)

    def agg(rows: list[dict[str, Any]], who: str) -> dict[str, int]:
        fs = [f for r in rows for f in r[who]["fields"]]
        at_risk = [f for f in fs if f["expected"]["status"] != "confirmed"]
        a = {"fields": len(fs), "correct": sum(f["correct"] for f in fs), "not_firm_fields": len(at_risk),
             "wrong_confirmed": sum(f["wrong_confirmed"] for f in fs)}
        if who == "chinku":
            a["status_correct"] = sum(bool(f["status_ok"]) for f in fs)
        return a

    groups = {"ALL": out, "real calls": [r for r in out if r["source"] == "real_call"],
              "scripted (simulated)": [r for r in out if r["source"].startswith("scripted")],
              "fresh (never used for tuning)": [r for r in out if r["fresh"]]}
    summary = {g: {"cases": len(rs), "chinku": agg(rs, "chinku"), "baseline": agg(rs, "baseline")} for g, rs in groups.items() if rs}
    (ROOT / "results.json").write_text(json.dumps({"summary": summary, "cases": out, "judge": JUDGE_LOG,
                                                   "models": {"brain": settings().openai_model_brain},
                                                   "baseline_prompt": BASELINE_PROMPT}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    pct = lambda a, b: f"{a}/{b} ({100 * a / b:.0f}%)" if b else "–"
    md = ["# Eval results (generated by eval/run_eval.py — do not edit)", "",
          f"Generated {time.strftime('%Y-%m-%d %H:%M')} · brain model `{settings().openai_model_brain}` · {len(cases)} cases · {time.time() - t0:.0f}s", "",
          f"Baseline = one plain prompt: *\"{BASELINE_PROMPT}\"* (asked to fill the same field names).", "",
          "| Set | Cases | Field accuracy — Chinku | Field accuracy — Baseline | Wrong-confirmed — Chinku | Wrong-confirmed — Baseline | Status accuracy — Chinku |",
          "|---|---|---|---|---|---|---|"]
    for g, s in summary.items():
        c, b = s["chinku"], s["baseline"]
        md.append(f"| {g} | {s['cases']} | {pct(c['correct'], c['fields'])} | {pct(b['correct'], b['fields'])} | "
                  f"{pct(c['wrong_confirmed'], c['not_firm_fields'])} | {pct(b['wrong_confirmed'], b['not_firm_fields'])} | {pct(c['status_correct'], c['fields'])} |")
    md += ["", "Wrong-confirmed = of the fields the person did NOT firmly state (tentative / not mentioned / refused), how many were reported as firm fact.", "",
           "## Per case", "", "| Case | Source | Chinku | Baseline | Notes |", "|---|---|---|---|---|"]
    for r in out:
        cc = sum(f["correct"] for f in r["chinku"]["fields"]); bb = sum(f["correct"] for f in r["baseline"]["fields"]); n = len(r["chinku"]["fields"])
        md.append(f"| {r['id']} | {r['source']} | {cc}/{n} | {bb}/{n} | {r['notes']} |")
    md += ["", "## Misses", ""]
    for r in out:
        for who in ("chinku", "baseline"):
            for f in r[who]["fields"]:
                if not f["correct"] or f["wrong_confirmed"]:
                    md.append(f"- **{who}** · {r['id']} · `{f['field']}`: expected {f['expected']['value']!r} ({f['expected']['status']}), "
                              f"got {f['got_value']!r}" + (f" ({f['got_status']})" if f["got_status"] else "") + (" — WRONG-CONFIRMED" if f["wrong_confirmed"] else ""))
    (ROOT / "results.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[6:6 + 2 + len(summary)]))


if __name__ == "__main__":
    main()
