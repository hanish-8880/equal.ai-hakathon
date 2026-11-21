"""Find a public contact (e.g. a brand's support email) with OpenAI web search. Always returns a source URL."""
import json
import re

from pydantic import BaseModel

from app.brain.client import oai
from app.config import settings
from app.store.dynamo import store


class Found(BaseModel):
    email: str | None = None
    source_url: str | None = None


def find_email(query: str, task_id: str | None = None) -> Found:
    r = oai().responses.create(
        model=settings().openai_model_brain, tools=[{"type": "web_search"}],
        input=f"{query}\nUse an official company website if possible. Return ONLY JSON "
              '{"email": "...", "source_url": "..."}; use null if not found. Never guess an address.')
    txt = r.output_text or ""
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        f = Found.model_validate(json.loads(m.group(0))) if m else Found()
    except Exception:  # noqa: BLE001
        f = Found()
    store().add_event(task_id or "-", "lookup", {"query": query, "out": f.model_dump()})
    return f
