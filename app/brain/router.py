import json
from typing import Any

from app.brain.client import json_call, prompt
from app.brain.schemas import RouteResult
from app.config import settings


def route(message: str, image: dict[str, Any] | None, catalog: list[dict[str, Any]],
          contacts: list[dict[str, Any]], task_id: str | None = None) -> RouteResult:
    s = settings()
    user = json.dumps({"message": message, "image": image, "spec_catalog": catalog, "contacts": contacts},
                      ensure_ascii=False)
    return json_call(s.openai_model_brain,  # brain model: misrouting costs more than 2-3 s
                      prompt("router").replace("{user_name}", s.user_name), user,
                     RouteResult, task_id, "route")
