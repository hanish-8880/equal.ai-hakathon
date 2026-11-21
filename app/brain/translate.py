"""Show Chinku's messages in the plumber's language (Telugu / Hindi / Telugu-English mix).
The LLM translates; CODE guards exactness: every number in the original (₹ amounts, quantities, times, job ids,
invoice numbers, model codes, masked phones) must appear unchanged in the translation, and the "(yes / no)"
answer hint must survive. Otherwise retry once with the brain model, then fall back to the English original —
a wrong amount is worse than English. Translations are cached, so repeated texts cost nothing."""
import hashlib
import re
from typing import Iterable

from pydantic import BaseModel

from app.config import settings
from app.store.dynamo import store

NAMES = {"te": "Telugu (Telugu script)", "hi": "Hindi (Devanagari script)",
         "te-en": "Telugu-English mix written in ENGLISH LETTERS (how a Hyderabad plumber texts on WhatsApp: Telugu sentences, common English words like job, bill, slot, shop, visit)"}
SCRIPT = {"te": re.compile(r"[ఀ-౿]"), "hi": re.compile(r"[ऀ-ॿ]")}
HINT = re.compile(r"\((?:yes|yes / no|yes / correct it)[^)]*\)", re.I)

SYSTEM = """You translate short messages from Chinku (an AI office assistant) to a plumber in Hyderabad.
Target language: {lang}.
Rules:
- Natural, short, spoken style — the way a helpful assistant would really say it. Not formal or bookish.
- Copy EXACTLY, character for character: every number and digit (amounts, ₹, quantities, times like 16:00–18:00, dates),
  job ids (J24), invoice/estimate numbers (INV-…, EST-…), work orders (WO-117), model codes (ALD-0517), masked phones (******3607),
  emails, URLs, emoji, people/shop/company names, and any text in parentheses that starts with "yes" such as (yes / no).
- Keep every line break where it is. Keep plumbing part names understandable (angle valve, hose, geyser may stay in English).
- Do not add or remove information.
Return JSON {{"text": "<translation>"}}."""


class Out(BaseModel):
    text: str


def code_of(user: str) -> str:
    from app.jobs import prefs
    return prefs.get(user).get("language") or "en"


def _digits(s: str) -> list[str]:
    return sorted(re.findall(r"\d+", s))


def safe(src: str, out: str) -> bool:
    """The exactness guard: same numbers, same answer hint."""
    if not out or _digits(src) != _digits(out):
        return False
    return all(h in out for h in HINT.findall(src))


def already(text: str, code: str) -> bool:
    rx = SCRIPT.get(code)
    if not rx:
        return False
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if rx.match(c)) / len(letters) > 0.3


def to_lang(text: str, code: str) -> str:
    if not text or not text.strip() or code not in NAMES or already(text, code):
        return text
    key = f"tr:{code}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"
    hit = store().get("contacts", key)
    if hit and hit.get("text"):
        return hit["text"]
    from app.brain.client import json_call
    s = settings()
    for model in [m for m in (s.openai_model_fast, s.openai_model_brain) if m]:
        try:
            out = json_call(model, SYSTEM.format(lang=NAMES[code]), text, Out, None, "translate").text
        except Exception:  # noqa: BLE001
            continue
        if safe(text, out):
            store().put("contacts", key, {"kind": "translation", "text": out})
            return out
        store().add_event("-", "translate_rejected", {"model": model, "code": code, "src": text[:300], "out": out[:300]})
    return text  # English original rather than a wrong number


def many(texts: Iterable[str], code: str) -> dict[str, str]:
    """Translate several short UI strings (job next steps etc.), cached individually."""
    return {t: to_lang(t, code) for t in {t for t in texts if t}}
