"""The ONLY OmniDimension code. Dispatch + webhook parsing.

Endpoint/field names are UNVERIFIED until scripts/omni_smoke.py succeeds (see DECISIONS.md).
"""
from typing import Any

import httpx

from app.config import settings


class OmniError(RuntimeError):
    pass


def dispatch_call(to_number: str, call_context: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
    s = settings()
    body: dict[str, Any] = {
        "agent_id": int(agent_id or s.omnidim_agent_id) if (agent_id or s.omnidim_agent_id).isdigit() else (agent_id or s.omnidim_agent_id),
        "to_number": to_number,
        "call_context": call_context,
    }
    if s.omnidim_from_number_id:
        body["from_number_id"] = int(s.omnidim_from_number_id) if s.omnidim_from_number_id.isdigit() else s.omnidim_from_number_id
    headers = {"Authorization": f"Bearer {s.omnidim_api_key}", "Content-Type": "application/json"}
    url = f"{s.omnidim_base_url}/calls/dispatch"
    last: Exception | None = None
    for _ in range(2):  # one retry
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=20)
            if r.status_code >= 400:
                raise OmniError(f"{r.status_code}: {r.text[:500]}")
            return r.json()
        except (httpx.HTTPError, OmniError) as e:
            last = e
    raise OmniError(str(last))


def parse_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise Omni post-call payload. Shape UNVERIFIED — fill from real payload logged in DECISIONS.md."""
    call_id = payload.get("call_id") or payload.get("id") or (payload.get("call_report") or {}).get("id")
    report = payload.get("call_report") or payload
    transcript = report.get("full_conversation") or report.get("transcript") or payload.get("transcript") or ""
    ctx = payload.get("call_context") or report.get("call_context") or {}
    if isinstance(transcript, list):  # list of turns -> "role: text" lines
        transcript = "\n".join(f"{t.get('role') or t.get('speaker')}: {t.get('content') or t.get('text')}" for t in transcript)
    return {
        "call_id": str(call_id) if call_id is not None else None,
        "chinku_call_id": ctx.get("chinku_call_id"),
        "transcript": transcript,
        "status": payload.get("call_status") or report.get("call_status") or payload.get("status"),
        "duration": payload.get("call_duration") or report.get("call_duration_in_seconds"),
        "extracted": report.get("extracted_variables") or {},
        "task_id": ctx.get("task_id"),
        "step_id": ctx.get("step_id"),
        "raw": payload,
    }
