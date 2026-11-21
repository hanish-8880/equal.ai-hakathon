"""Create the inbound agent `chinku-inbound` and point the phone number's incoming calls at it.
Run once: python -m scripts.omni_setup_inbound
"""
import json
from pathlib import Path

import httpx

from app.config import settings

s = settings()
H = {"Authorization": f"Bearer {s.omnidim_api_key}"}
B = s.omnidim_base_url
prompt = Path("app/brain/prompts/inbound_agent.md").read_text(encoding="utf-8")
body = {
    "name": "chinku-inbound",
    "welcome_message": "Hello! Chinku here — Hanish's assistant. Cheppandi, em cheyyali?",
    "context_breakdown": [{"title": "Chinku inbound — take the task", "body": prompt}],
    "call_type": "Incoming",
    "transcriber": {"provider": "soniox", "languages": ["te", "en", "hi"]},
    "model": {"model": "gpt-4.1", "temperature": 0.3},
    "voice": {"provider": "cartesia", "voice_id": "9cebb910-d4b7-4a4a-85a4-12c79137724c"},
    "languages": ["English (India)", "Telugu", "Hindi"],
    "post_call_actions": {"webhook": {"enabled": True, "url": f"{s.public_base_url}/webhooks/omni",
                                      "include": ["summary", "fullConversation", "sentiment", "extracted_variables"]}},
}
r = httpx.post(f"{B}/agents/create", json=body, headers=H, timeout=60)
print("create", r.status_code, r.text[:300])
agent_id = r.json().get("id")
print(json.dumps({"agent_id": agent_id}))
