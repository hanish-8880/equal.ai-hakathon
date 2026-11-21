"""One OpenAI wrapper: JSON output validated by Pydantic, 1 retry on invalid JSON, timeout, logging."""
import base64
import json
from pathlib import Path
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings

T = TypeVar("T", bound=BaseModel)
PROMPTS = Path(__file__).parent / "prompts"
_client: OpenAI | None = None


def oai() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings().openai_api_key, timeout=60, max_retries=1)
    return _client


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def _log(task_id: str | None, kind: str, payload: dict[str, Any]) -> None:
    from app.store.dynamo import store  # late import, avoid cycle
    store().add_event(task_id or "-", kind, payload)


def json_call(model: str, system: str, user: str | list[dict[str, Any]], schema: type[T],
              task_id: str | None = None, tag: str = "llm", effort: str | None = None) -> T:
    schema_hint = json.dumps(schema.model_json_schema())
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\nReturn ONLY a JSON object matching this JSON schema:\n{schema_hint}"},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for attempt in range(2):
        extra = {"reasoning_effort": effort} if effort and model.startswith(("gpt-5", "o")) else {}
        r = oai().chat.completions.create(model=model, messages=messages, response_format={"type": "json_object"}, **extra)
        raw = r.choices[0].message.content or ""
        try:
            out = schema.model_validate_json(raw)
            _log(task_id, tag, {"model": model, "attempt": attempt, "out": raw[:2000]})
            return out
        except ValidationError as e:
            last_err = str(e)[:500]
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Invalid JSON for the schema: {last_err}. Return corrected JSON only."}]
    _log(task_id, f"{tag}_error", {"model": model, "error": last_err})
    raise ValueError(f"{tag}: invalid JSON after retry: {last_err}")


def image_part(data: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
