"""FIRST-HOUR VERIFICATION (CLAUDE.md §4). Dispatches one test call to TEST_PHONE.
Expected: agent says "Chinku test number 4 7 2" then ends.
Run: python -m scripts.omni_smoke
"""
import json

from app.config import settings
from app.voice.omni import dispatch_call

s = settings()
assert s.test_phone.startswith("+"), "Set TEST_PHONE in .env (E.164, leading +)"
ctx = {
    "on_behalf_of": s.user_name,
    "language": "English",
    "max_minutes": 1,
    "call_script": "Say only: Chinku test number 4 7 2, then end the call.",
    "task_id": "smoke",
    "step_id": "smoke",
}
print(json.dumps(dispatch_call(s.test_phone, ctx), indent=2))
