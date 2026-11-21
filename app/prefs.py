"""Per-user preferences (language chosen on first run)."""
from app.config import settings
from app.store.dynamo import store

LANGUAGES = {"en": "English", "te": "Telugu", "hi": "Hindi", "te-en": "Telugu-English mix (written in English letters)"}


def get(user: str) -> dict:
    return store().get("contacts", f"prefs:{user}") or {}


def language(user: str) -> str:
    code = get(user).get("language")
    return LANGUAGES.get(code, settings().default_language)


def set_language(user: str, code: str) -> None:
    if code not in LANGUAGES:
        raise ValueError(code)
    p = get(user)
    store().put("contacts", f"prefs:{user}", {**p, "owner": user, "kind": "prefs", "language": code})
