"""The ONLY OmniDimension code. Dispatch + webhook parsing.

Endpoint/field names are UNVERIFIED until scripts/omni_smoke.py succeeds (see DECISIONS.md).
"""
import re
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


def _clean_conv(t: Any) -> str:
    if isinstance(t, list):  # list of turns -> "role: text" lines
        return "\n".join(f"{x.get('role') or x.get('speaker')}: {x.get('content') or x.get('text')}" for x in t)
    s = str(t or "")
    s = re.sub(r"<br\s*/?>", "\n", s)
    return s.replace("LLM:", "assistant:").replace("User:", "user:").strip()


def parse_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise an Omni post-call webhook OR a call-log row (VERIFIED shape for logs, see DECISIONS.md).
    Log row: call_conversation ("LLM: ..<br/>User: .."), custom_variables (our call_context),
    call_request_id.id (= dispatch requestId), call_status, call_duration_in_seconds."""
    report = payload.get("call_report") or payload
    req = report.get("call_request_id") or payload.get("call_request_id") or {}
    call_id = (req.get("id") if isinstance(req, dict) else req) or payload.get("requestId") \
        or payload.get("call_id") or report.get("id") or payload.get("id")
    transcript = _clean_conv(report.get("call_conversation") or report.get("full_conversation")
                             or report.get("transcript") or payload.get("transcript"))
    ctx = (payload.get("call_context") or report.get("call_context") or report.get("custom_variables")
           or payload.get("custom_variables") or {})
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


def fetch_log(request_id: str) -> dict[str, Any] | None:
    """Polling fallback: find the call-log row for a dispatch requestId."""
    s = settings()
    r = httpx.get(f"{s.omnidim_base_url}/calls/logs", params={"pageno": 1, "pagesize": 30, "agentid": s.omnidim_agent_id},
                  headers={"Authorization": f"Bearer {s.omnidim_api_key}"}, timeout=20)
    r.raise_for_status()
    for row in r.json().get("call_log_data", []):
        rid = (row.get("call_request_id") or {}).get("id")
        if str(rid) == str(request_id):
            return row
    return None
