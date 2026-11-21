"""The ONLY step types: call | wait_for | email | schedule | reply."""
import threading
import time
import uuid
from typing import Any

from app.brain import brief_writer, decider, extractor
from app.engine.specs_loader import arith, render
from app.store.dynamo import store
from app.voice import omni

TERMINAL = {"completed", "no_answer", "failed"}
RETRY_DELAY_S = 300
DISPATCH = omni.dispatch_call  # swapped in tests


def call_targets(spec: dict[str, Any], task: dict[str, Any], step: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    step = step or next(s for s in spec["steps"] if s["type"] == "call")
    items = task["inputs"].get(step["for_each"], []) if step.get("for_each") else [None]
    out = []
    for it in items:
        ctx = {"inputs": task["inputs"], "item": it or {}}
        out.append({"to": render(step["to"], ctx), "callee": render(step.get("callee", {}), ctx), "item": it})
    return out


# ---------------- call ----------------
def _prior_results(task: dict[str, Any]) -> dict[str, Any]:
    return {k: v.get("output") for k, v in task.get("steps", {}).items() if v.get("status") == "done"}


def dispatch_one(task: dict[str, Any], step: dict[str, Any], target: dict[str, Any], attempt: int = 0) -> dict[str, Any]:
    cid = "c_" + uuid.uuid4().hex[:10]
    call = {"call_id": cid, "task_id": task["task_id"], "step_id": step["id"], "to_number": target["to"],
            "callee": target["callee"], "attempt": attempt, "status": "queued", "created_at": time.time()}
    store().put("calls", cid, call)
    try:
        brief = brief_writer.write_brief(step, task["inputs"], target["callee"], _prior_results(task), task["task_id"])
        call["brief"] = brief.model_dump()
        ctx = {**{k: (v if isinstance(v, (str, int, float)) else str(v)) for k, v in brief.model_dump().items()
                  if k in ("on_behalf_of", "language", "max_minutes", "call_script", "callee_role")},
               "task_id": task["task_id"], "step_id": step["id"], "chinku_call_id": cid}
        resp = DISPATCH(target["to"], ctx)
        call["omni"] = resp
        call["omni_call_id"] = str(resp.get("requestId") or resp.get("call_id") or resp.get("id") or "")
        call["status"] = "dispatched"
        store().add_event(task["task_id"], "call_dispatched", {"call_id": cid, "to": target["to"][-4:], "omni": resp})
    except Exception as e:  # noqa: BLE001 — any dispatch failure ends this call as failed
        call["status"] = "failed"
        call["error"] = str(e)[:500]
        store().add_event(task["task_id"], "call_failed", {"call_id": cid, "error": call["error"]})
    store().put("calls", cid, call)
    return call


def run_call(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    targets = call_targets(spec, task, step)
    # mark running BEFORE dispatch so fast webhooks see a running step
    t = store().get("tasks", task["task_id"])
    t["steps"][step["id"]] = {"status": "running"}
    store().put("tasks", t["task_id"], t)
    threads = [threading.Thread(target=dispatch_one, args=(task, step, tg)) for tg in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if step_calls_terminal(task["task_id"], step["id"]):  # e.g. all dispatches failed
        return {"status": "done", "output": collect(task["task_id"], step["id"])}
    return {"status": "running"}


def find_call(parsed: dict[str, Any]) -> dict[str, Any] | None:
    raw_ctx = (parsed.get("raw") or {}).get("call_context") or {}
    cid = parsed.get("chinku_call_id") or raw_ctx.get("chinku_call_id")
    if cid:
        return store().get("calls", cid)
    if parsed.get("call_id"):
        hits = store().scan("calls", omni_call_id=parsed["call_id"])
        if hits:
            return hits[0]
    if parsed.get("task_id") and parsed.get("step_id"):
        hits = [c for c in store().scan("calls", task_id=parsed["task_id"], step_id=parsed["step_id"])
                if c["status"] == "dispatched"]
        if len(hits) == 1:
            return hits[0]
    return None


def record_call_result(parsed: dict[str, Any]) -> dict[str, Any] | None:
    call = find_call(parsed)
    if not call:
        store().add_event("-", "webhook_unmatched", {"call_id": parsed.get("call_id")})
        return None
    if call["status"] in TERMINAL:
        return None  # duplicate after terminal: never re-process
    task = store().get("tasks", call["task_id"])
    from app.engine.specs_loader import load_specs
    spec = load_specs()[task["spec"]]
    step = next(s for s in spec["steps"] if s["id"] == call["step_id"])
    transcript = parsed.get("transcript") or ""
    call["transcript"] = transcript
    if not transcript.strip() or parsed.get("status") in ("no-answer", "busy", "failed", "no_answer"):
        call["status"], call["outcome"] = "no_answer", "no_answer"
    else:
        ex = extractor.extract(transcript, step["must_collect"],
                               {k: spec["result_schema"][k] for k in step["must_collect"]}, task["task_id"])
        call["extracted"] = ex.model_dump()
        call["outcome"] = ex.call_outcome
        call["status"] = "no_answer" if ex.call_outcome == "no_answer" else "completed"
    call["duration"] = parsed.get("duration")
    store().put("calls", call["call_id"], call)
    store().add_event(call["task_id"], "call_result", {"call_id": call["call_id"], "outcome": call["outcome"]})
    return call


def maybe_retry(call: dict[str, Any], step: dict[str, Any], task: dict[str, Any]) -> bool:
    if call["status"] != "no_answer" or call["attempt"] >= int(step.get("retry_on_no_answer", 0)):
        return False
    call["superseded"] = True
    store().put("calls", call["call_id"], call)
    target = {"to": call["to_number"], "callee": call["callee"]}
    threading.Timer(RETRY_DELAY_S, dispatch_one, args=(task, step, target, call["attempt"] + 1)).start()
    store().add_event(task["task_id"], "call_retry_scheduled", {"call_id": call["call_id"], "in_s": RETRY_DELAY_S})
    return True


def step_calls_terminal(task_id: str, step_id: str) -> bool:
    calls = [c for c in store().scan("calls", task_id=task_id, step_id=step_id) if not c.get("superseded")]
    return bool(calls) and all(c["status"] in TERMINAL for c in calls)


def collect(task_id: str, step_id: str) -> list[dict[str, Any]]:
    out = []
    for c in sorted(store().scan("calls", task_id=task_id, step_id=step_id), key=lambda c: c["created_at"]):
        if c.get("superseded"):
            continue
        fields = {f["name"]: f for f in (c.get("extracted") or {}).get("fields", [])}
        out.append({"callee": c["callee"], "to_last4": c["to_number"][-4:] if c.get("to_number") else None,
                    "status": c["status"], "outcome": c.get("outcome"), "fields": fields})
    return out


# ---------------- reply ----------------
def rank(results: list[dict[str, Any]], expr: str, inputs: dict[str, Any], schema: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Deterministic comparison. The LLM never computes money."""
    rows = []
    for r in results:
        names = {k: v for k, v in inputs.items() if isinstance(v, (int, float))}
        for k, cfg in schema.items():
            f = r["fields"].get(k) or {}
            v = f.get("value") if f.get("status") in ("confirmed", "tentative") else None
            if v is None and "default" in cfg:
                v = cfg["default"]
            if isinstance(v, str):
                try:
                    v = float(v.replace(",", ""))
                except ValueError:
                    v = None
            names[k] = v
        v = arith(expr, names)
        v = int(v) if v is not None and float(v).is_integer() else v
        rows.append({"callee": r["callee"].get("name") or r["callee"].get("role"), label: v,
                     "tentative": any(f.get("status") == "tentative" for f in r["fields"].values())})
    priced = sorted([x for x in rows if x[label] is not None], key=lambda x: x[label])
    return priced + [x for x in rows if x[label] is None]


def run_reply(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    prior = _prior_results(task)
    results = [r for out in prior.values() if isinstance(out, list) for r in out]
    ranking = rank(results, step["rank_by"], task["inputs"], spec.get("result_schema", {}),
                   step.get("rank_label", "score")) if step.get("rank_by") else None
    text = decider.compose_reply(step.get("template_hint", ""), task["inputs"], results, ranking,
                                 task.get("language", "Telugu-English mix"), task["task_id"])
    send(text)
    return {"status": "done", "output": {"text": text, "ranking": ranking}}


def run(step: dict[str, Any], spec: dict[str, Any], task: dict[str, Any], send) -> dict[str, Any]:
    if step["type"] == "call":
        return run_call(step, spec, task)
    if step["type"] == "reply":
        return run_reply(step, spec, task, send)
    raise NotImplementedError(f"step type {step['type']} NOT BUILT yet")
