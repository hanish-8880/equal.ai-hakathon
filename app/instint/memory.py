"""Personal memory (CLAUDE.md §8): contacts, places, preferences, past tickets — used by the planner automatically.
NEVER stores OTPs, passwords, card or bank account numbers, or government ID numbers (checked in code)."""
import re
import time
from typing import Any

from app.store.dynamo import store

SECRET = [
    (re.compile(r"\b(otp|one[- ]time password|password|passcode|pin|cvv|upi pin)\b", re.I), "a password / OTP / PIN"),
    (re.compile(r"\b(?:\d[ -]?){15,16}\b"), "a card number"),
    (re.compile(r"(?<![+\d])\b\d{4}[ -]\d{4}[ -]\d{4}\b|\b(aadhaar|aadhar|uid|voter id|passport)\b", re.I), "a government ID number"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "a PAN number"),
    (re.compile(r"\b(account|a/c|acct)\s*(no\.?|number)?\s*[:#]?\s*\d{9,18}\b", re.I), "a bank account number"),
    (re.compile(r"\bifsc\b", re.I), "bank details"),
]


def secret_reason(text: str) -> str | None:
    for rx, why in SECRET:
        if rx.search(text or ""):
            return why
    return None


def _norm(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (k or "").lower()).strip("_")[:60]


def remember(user: str, key: str, value: Any, kind: str = "fact") -> dict[str, Any]:
    blob = f"{key} {value}"
    why = secret_reason(blob)
    if why:
        return {"saved": False, "reason": f"I never store {why}."}
    k = _norm(key)
    if not k:
        return {"saved": False, "reason": "nothing to remember"}
    store().put("contacts", f"mem:{user}:{k}", {"owner": user, "kind": "memory", "mem_kind": kind, "key": key, "value": value,
                                               "ts": time.time()})
    return {"saved": True, "key": key, "value": value}


def recall(user: str) -> list[dict[str, Any]]:
    rows = [r for r in store().scan("contacts", kind="memory") if r.get("owner") == user]
    return sorted(({"key": r["key"], "value": r["value"], "kind": r.get("mem_kind", "fact")} for r in rows), key=lambda r: r["key"])


def forget(user: str, key: str) -> bool:
    k = f"mem:{user}:{_norm(key)}"
    if store().get("contacts", k) is None:
        return False
    store().delete("contacts", k)
    return True


def get(user: str, key: str) -> Any:
    r = store().get("contacts", f"mem:{user}:{_norm(key)}")
    return r["value"] if r else None
