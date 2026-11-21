import json
from typing import Any

from app.brain.client import json_call, prompt
from app.brain.schemas import CallBrief
from app.config import settings


def write_brief(step: dict[str, Any], inputs: dict[str, Any], callee: dict[str, Any],
                prior_results: dict[str, Any], task_id: str | None = None) -> CallBrief:
    s = settings()
    user = json.dumps({
        "on_behalf_of": s.user_name,
        "step_goal": step["goal"],
        "must_collect": step["must_collect"],
        "language": step.get("language", "Telugu-English mix"),
        "max_minutes": step.get("max_minutes", 3),
        "callee": callee,
        "task_inputs": inputs,
        "prior_results": prior_results,
        "shareable": step.get("shareable", []),
    }, ensure_ascii=False)
    brief = json_call(s.openai_model_brain, prompt("brief_writer").replace("{on_behalf_of}", s.user_name),
                      user, CallBrief, task_id, "brief")
    # deterministic guards: must_collect fields exactly as spec; disclosure present
    want = list(step["must_collect"])
    got = [m.field for m in brief.must_collect]
    if sorted(want) != sorted(got):
        raise ValueError(f"brief must_collect mismatch: want {want}, got {got}")
    brief.on_behalf_of = s.user_name
    brief.max_minutes = int(step.get("max_minutes", 3))
    return brief
