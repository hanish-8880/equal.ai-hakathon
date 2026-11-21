"""Voice → text (CLAUDE.md §4 transcribe_audio): Telugu / English / Hindi mixed, vocabulary prompt built per request
from the user's own memory (names, places, contacts), low-confidence words kept for the "what I heard" bubble."""
import io
import re

from app.brain.schemas import Heard
from app.config import settings
from app.store.dynamo import store


def vocabulary(user: str) -> str:
    from app.instint import memory
    names = []
    for r in memory.recall(user):
        for x in (r["key"], str(r["value"])):
            if x and "@" not in x and not re.search(r"\d{6,}", x):
                names.append(x)
    known = ", ".join(dict.fromkeys(names))[:600]
    return ("The speaker mixes Telugu, English and Hindi. Common words: email, remind, call, book, doctor, complaint, ticket, "
            "transcribe, Mummy, Nanna, Akka. " + (f"Names and places: {known}." if known else ""))


def _languages(text: str) -> list[str]:
    langs = []
    if re.search(r"[ఀ-౿]", text):
        langs.append("te")
    if re.search(r"[ऀ-ॿ]", text):
        langs.append("hi")
    if re.search(r"[A-Za-z]", text):
        langs.append("en")
    return langs


def transcribe(data: bytes, filename: str, user: str, source: str, audio_ref: str | None = None) -> Heard | None:
    """None for silent / empty audio (caller replies "I didn't hear anything, try again")."""
    from app.brain.client import oai
    s = settings()
    if len(data) < 1200:
        return None
    f = io.BytesIO(data)
    f.name = filename
    r = oai().audio.transcriptions.create(model=s.openai_model_transcribe, file=f, response_format="json",
                                         prompt=vocabulary(user), include=["logprobs"])
    text = (r.text or "").strip()
    if len(re.sub(r"[\W_]", "", text)) < 2:
        return None
    low, cur = [], ""
    for tok in getattr(r, "logprobs", None) or []:  # consecutive low-probability tokens → one span
        if getattr(tok, "logprob", 0) < -1.0:
            cur += tok.token
        elif cur:
            low.append(cur.strip())
            cur = ""
    if cur:
        low.append(cur.strip())
    words = re.findall(r"[\wఀ-౿ऀ-ॿ‌-]+", text)
    low_words: list[str] = []
    for frag in low:
        frag = frag.strip(" .,")
        hit = next((w for w in words if frag and frag in w), None)
        if hit and hit not in low_words:
            low_words.append(hit)
    heard = Heard(text=text, languages=_languages(text), low_confidence_spans=low_words,
                  model=s.openai_model_transcribe, audio_ref=audio_ref, source=source)
    store().add_event("-", "heard", heard.model_dump())
    return heard
