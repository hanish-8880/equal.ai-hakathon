import json
from typing import Any

from app.brain.client import json_call, prompt
from app.brain.schemas import Extraction
from app.config import settings


def extract(transcript: str, must_collect: list[str], result_schema: dict[str, Any],
            task_id: str | None = None) -> Extraction:
    user = json.dumps({"must_collect": must_collect, "result_schema": result_schema, "transcript": transcript},
                      ensure_ascii=False)
    ex = json_call(settings().openai_model_brain, prompt("extractor"), user, Extraction, task_id, "extract")
    # deterministic guard: a value without an evidence quote can never be "confirmed"
    names = {f.name for f in ex.fields}
    for f in ex.fields:
        if f.status == "confirmed" and not f.evidence_quote:
            f.status = "tentative"
    from app.brain.schemas import ExtractedField
    for m in must_collect:
        if m not in names:
            ex.fields.append(ExtractedField(name=m, status="not_mentioned"))
    ex.follow_up_needed = any(f.status != "confirmed" for f in ex.fields if f.name in must_collect)
    return ex
