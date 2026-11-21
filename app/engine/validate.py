import re
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def indian_mobile(v: Any) -> str | None:
    """Normalise to E.164 +91XXXXXXXXXX (10 digits, starts 6-9). None if invalid."""
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10 and d[0] in "6789":
        return "+91" + d
    return None


def email(v: Any) -> str | None:
    return str(v).strip() if v and EMAIL_RE.match(str(v).strip()) else None


def phone_kind(v: Any) -> tuple[str | None, str | None]:
    """(+91…, kind) for numbers Chinku may dial: mobile, tollfree (1800/1860/1600 helplines), landline (0 + STD code).
    '0891-2345678' is a landline (separator after the STD code), never a mobile; '098480 22222' is a mobile."""
    if v is None:
        return None, None
    raw = str(v).strip()
    d = re.sub(r"\D", "", raw)
    if d.startswith("91") and len(d) in (13, 14) and d[2:6] in ("1800", "1860", "1600"):
        d = d[2:]
    if d[:4] in ("1800", "1860", "1600") and 10 <= len(d) <= 11:
        return "+91" + d, "tollfree"
    if re.match(r"^\s*(?:\+?91[\s-]?)?0\d{2,4}[\s-]", raw):  # 0 + STD code + separator → landline
        core = re.sub(r"\D", "", re.sub(r"^\s*\+?91[\s-]?", "", raw)).lstrip("0")
        if len(core) == 10:
            return "+91" + core, "landline"
    m = indian_mobile(raw)
    return (m, "mobile") if m else (None, None)


def dialable(v: Any) -> str | None:
    return phone_kind(v)[0]


VALIDATORS = {"indian_mobile": indian_mobile, "email": email, "dialable": dialable}


def mask_phone(p: str | None) -> str:
    return ("*" * 6 + p[-4:]) if p and len(p) >= 4 else "?"
