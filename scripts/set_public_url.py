"""After the tunnel URL changes: update .env PUBLIC_BASE_URL and both Omni agents' post-call webhooks.
Run: python -m scripts.set_public_url https://<new>.trycloudflare.com
(Twilio sandbox "When a message comes in" must still be changed by hand in the Twilio console.)
"""
import re
import sys
from pathlib import Path

import httpx

url = sys.argv[1].rstrip("/")
assert url.startswith("https://"), "pass the https tunnel URL"
env = Path(".env")
s = env.read_text(encoding="utf-8")
s = re.sub(r"^PUBLIC_BASE_URL=.*$", f"PUBLIC_BASE_URL={url}", s, flags=re.M)
env.write_text(s, encoding="utf-8")

from app.config import settings  # noqa: E402  (read after .env update)

cfg = settings()
H = {"Authorization": f"Bearer {cfg.omnidim_api_key}"}
hook = {"post_call_actions": {"webhook": {"enabled": True, "url": f"{url}/webhooks/omni",
                                          "include": ["summary", "fullConversation", "sentiment", "extracted_variables"]}}}
for agent in filter(None, [cfg.omnidim_agent_id, cfg.omnidim_inbound_agent_id]):
    r = httpx.put(f"{cfg.omnidim_base_url}/agents/{agent}", json=hook, headers=H, timeout=30)
    d = httpx.get(f"{cfg.omnidim_base_url}/agents/{agent}", headers=H, timeout=30).json()
    print(agent, d.get("name"), r.status_code, [p.get("webhook_url") for p in d.get("post_call_config_ids") or []])
try:
    print("health via tunnel:", httpx.get(f"{url}/health", timeout=15).text)
except httpx.HTTPError as e:
    print("tunnel not reachable yet:", e)
print(f"\nNOW set Twilio sandbox 'When a message comes in' = {url}/webhooks/whatsapp (POST)")
