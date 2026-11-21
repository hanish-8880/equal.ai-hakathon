"""Create the single generic Omni agent `chinku-caller` from prompts/base_agent.md.
Run once: python -m scripts.omni_create_agent   (prints the new agent id)
"""
import json
from pathlib import Path

import httpx

from app.config import settings

s = settings()
prompt = Path("app/brain/prompts/base_agent.md").read_text(encoding="utf-8")
body = {
    "name": "chinku-caller",
    "welcome_message": "Namaste, I am Chinku, an AI assistant calling for {{on_behalf_of}}.",
    "context_breakdown": [{"title": "Chinku base rules", "body": prompt}],
    "call_type": "Outgoing",
    "transcriber": {"provider": "deepgram_stream", "model": "nova-3", "silence_timeout_ms": 400},
    "model": {"model": "gpt-4.1-mini", "temperature": 0.3},
    "voice": {"provider": "cartesia", "voice_id": "c7bbe380-8e6c-4b01-8905-f107f41b1afd"},
    "languages": ["English (India)", "Telugu", "Hindi"],
}
if s.public_base_url:
    body["post_call_actions"] = {
        "webhook": {"enabled": True, "url": f"{s.public_base_url}/webhooks/omni", "include": ["summary", "fullConversation", "sentiment", "extracted_variables"]}
    }
r = httpx.post(f"{s.omnidim_base_url}/agents/create", json=body,
               headers={"Authorization": f"Bearer {s.omnidim_api_key}"}, timeout=60)
print(r.status_code)
print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:3000])
