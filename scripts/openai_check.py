"""Hour-1 check: list models available on our key and test the configured ones.
Run: python -m scripts.openai_check
"""
from openai import OpenAI

from app.config import settings

s = settings()
c = OpenAI(api_key=s.openai_api_key)
ids = sorted(m.id for m in c.models.list())
print("Available (filtered):", [i for i in ids if any(k in i for k in ("gpt", "o1", "o3", "o4", "whisper", "transcribe", "tts"))])
for name in ("openai_model_brain", "openai_model_fast", "openai_model_transcribe", "openai_model_tts"):
    v = getattr(s, name)
    print(f"{name}={v!r} ->", "OK" if v in ids else "NOT AVAILABLE / unset")
if s.openai_model_fast in ids:
    r = c.chat.completions.create(model=s.openai_model_fast, messages=[{"role": "user", "content": "Reply with JSON {\"ok\": true}"}], response_format={"type": "json_object"})
    print("fast model JSON test:", r.choices[0].message.content)
