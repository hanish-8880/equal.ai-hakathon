"""Push the Instint persona to both live OmniDimension agents (prompts + welcome lines). Names/ids stay the same.
Run: python -m scripts.omni_sync_agents"""
from pathlib import Path

import httpx

from app.config import settings

s = settings()
H = {"Authorization": f"Bearer {s.omnidim_api_key}"}
P = Path("app/brain/prompts")
agents = {
    s.omnidim_agent_id: {"welcome_message": "Hello, I'm Instint, an AI assistant calling on behalf of {{on_behalf_of}}.",
                         "context_breakdown": [{"title": "Instint base rules", "body": (P / "base_agent.md").read_text(encoding="utf-8")}]},
    s.omnidim_inbound_agent_id: {"welcome_message": f"Hello {s.user_name}! Instint here. What do you need?",
                                 "context_breakdown": [{"title": "Instint inbound — take the request",
                                                        "body": (P / "inbound_agent.md").read_text(encoding="utf-8").replace("{user_name}", s.user_name)}]},
}
for aid, body in agents.items():
    r = httpx.put(f"{s.omnidim_base_url}/agents/{aid}", json=body, headers=H, timeout=30)
    d = httpx.get(f"{s.omnidim_base_url}/agents/{aid}", headers=H, timeout=30).json()
    d = d.get("json", d)
    cb = d.get("context_breakdown") or []
    print(aid, r.status_code, "| welcome:", d.get("welcome_message"), "| prompt starts:",
          (cb[0].get("context_body") or "")[:70].replace("\n", " ") if cb else None)
