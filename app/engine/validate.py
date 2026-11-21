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


VALIDATORS = {"indian_mobile": indian_mobile, "email": email}


def mask_phone(p: str | None) -> str:
    return ("*" * 6 + p[-4:]) if p and len(p) >= 4 else "?"
