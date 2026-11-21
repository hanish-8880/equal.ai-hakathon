"""Show Instint's messages in the user's language (Telugu / Hindi / Telugu-English mix).
The LLM translates; CODE guards exactness: every number in the original (₹ amounts, times, dates, ticket numbers,
masked phones) must appear unchanged in the translation, and the "(yes / no)" answer hint must survive.
Otherwise retry once with the brain model, then fall back to the English original — a wrong number is worse than
English. Translations are cached, so repeated texts cost nothing."""
import hashlib
import re
from typing import Iterable

from pydantic import BaseModel

from app.config import settings
from app.store.dynamo import store

NAMES = {"te": "Telugu (Telugu script)", "hi": "Hindi (Devanagari script)",
         "te-en": "Telugu-English mix written in ENGLISH LETTERS (how people in Hyderabad text on WhatsApp: Telugu sentences, common English words like email, call, ticket, booking, reminder)"}
SCRIPT = {"te": re.compile(r"[ఀ-౿]"), "hi": re.compile(r"[ऀ-ॿ]")}
HINT = re.compile(r"\((?:yes|yes / no|yes / correct it)[^)]*\)", re.I)

SYSTEM = """You translate short messages from Instint (a personal AI assistant) to its user in India.
Target language: {lang}.
Rules:
- TRANSLATE EVERY SENTENCE fully into the target language. Only the protected items below stay as they are.
- Natural, short, spoken style — the way a helpful local assistant really talks. Not formal or bookish.
- PROTECTED — copy character for character, never transliterate: every number and digit (amounts, ₹, times, dates),
  ticket / order / reference numbers, masked phones (******3607), emails, URLs, emoji, names of people, clinics, shops and
  companies in Latin letters (Suresh, Sri Sai Clinic), and any text in parentheses that starts with "yes" such as (yes / no).
- Common English words people use as-is may stay (email, ticket, booking, OTP) when that is how people really say it.
- Keep every line break where it is. Do not add or remove information.
Example (Telugu): "Booked · Tomorrow, Sun 22 Nov, 6:00 PM · Sri Sai Clinic.\nSend this email to suresh@example.com? (yes / no)"
→ "బుక్ అయ్యింది · రేపు, ఆది 22 నవం, 6:00 PM · Sri Sai Clinic.\nఈ ఇమెయిల్ suresh@example.com కి పంపాలా? (yes / no)"
Example (Hindi): same → "बुक हो गया · कल, रवि 22 नवं, 6:00 PM · Sri Sai Clinic।\nयह ईमेल suresh@example.com को भेजूँ? (yes / no)"
Example (Telugu-English mix): same → "Book ayyindi · Repu, Sun 22 Nov, 6:00 PM · Sri Sai Clinic.\nEe email suresh@example.com ki pampala? (yes / no)"
Return JSON {{"text": "<translation>"}}."""


class Out(BaseModel):
    text: str


def code_of(user: str) -> str:
    from app import prefs
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


def _key(text: str, code: str) -> str:
    return f"tr4:{code}:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def cached(text: str, code: str) -> str | None:
    if not text or not text.strip() or code not in NAMES or already(text, code):
        return text
    hit = store().get("contacts", _key(text, code))
    return hit["text"] if hit and hit.get("text") else None


def to_lang(text: str, code: str) -> str:
    # a message that is mostly in the target script can still carry English lines (e.g. a code-built step list)
    if text and code in SCRIPT and "\n" in text and already(text, code):
        lines = text.split("\n")
        todo = [i for i, ln in enumerate(lines) if any(c.isalpha() for c in ln) and not already(ln, code)]
        if todo:
            out = to_lang("\n".join(lines[i] for i in todo), code).split("\n")
            if len(out) == len(todo):
                for i, ln in zip(todo, out):
                    lines[i] = ln
                return "\n".join(lines)
        return text
    hit = cached(text, code)
    if hit is not None:
        return hit
    key = _key(text, code)
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


_pending: set[str] = set()


def many(texts: Iterable[str], code: str) -> dict[str, str]:
    """Short UI strings (job next steps): cached ones now; the rest are translated in the background
    (in parallel) and appear on the app's next refresh — the jobs screen never waits on the model."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    out, todo = {}, []
    for t in {t for t in texts if t}:
        hit = cached(t, code)
        out[t] = hit if hit is not None else t
        if hit is None and _key(t, code) not in _pending:
            _pending.add(_key(t, code))
            todo.append(t)
    if todo:
        def work():
            try:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    list(ex.map(lambda t: to_lang(t, code), todo))
            finally:
                for t in todo:
                    _pending.discard(_key(t, code))
        threading.Thread(target=work, daemon=True).start()
    return out
