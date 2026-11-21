import json
from typing import Any

from app.brain.client import json_call, prompt
from app.brain.schemas import EmailDraft, Reply
from app.config import settings


def compose_reply(hint: str, inputs: dict[str, Any], results: list[dict[str, Any]],
                  ranking: list[dict[str, Any]] | None, language: str, task_id: str | None = None) -> str:
    s = settings()
    user = json.dumps({"instruction": hint, "inputs": inputs, "RESULTS": results, "RANKING": ranking},
                      ensure_ascii=False, default=str)
    sys = prompt("reply").replace("{user_name}", s.user_name).replace("{language}", language)
    return json_call(s.openai_model_brain, sys, user, Reply, task_id, "reply").text


def draft_email(goal: str, inputs: dict[str, Any], shareable: list[str], task_id: str | None = None) -> EmailDraft:
    s = settings()
    user = json.dumps({"goal": goal, "task_inputs": inputs, "shareable": shareable}, ensure_ascii=False, default=str)
    sys = prompt("email_writer").replace("{user_name}", s.user_name)
    return json_call(s.openai_model_brain, sys, user, EmailDraft, task_id, "email_draft")
