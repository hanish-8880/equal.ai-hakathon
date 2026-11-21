"""Voice → text (CLAUDE.md §4 transcribe_audio): Telugu / English / Hindi mixed, vocabulary prompt built per request
from the user's own memory (names, places, contacts), low-confidence words kept for the "what I heard" bubble."""
import io
import re

from app.brain.schemas import Heard
from app.config import settings
from app.store.dynamo import store


def vocabulary(user: str) -> str:
    from app.chinku import memory
    names = []
    for r in memory.recall(user):
        for x in (r["key"], str(r["value"])):
            if x and "@" not in x and not re.search(r"\d{6,}", x):
                names.append(x)
    known = ", ".join(dict.fromkeys(names))[:600]
    return ("The speaker mixes Telugu, English and Hindi. Common words: email, remind, call, book, doctor, complaint, ticket, "
            "transcribe, Mummy, Nanna, Akka. " + (f"Names and places: {known}." if known else ""))


LEAK = re.compile(r"context\s*:|###|the speaker mixes|common words|names and places", re.I)


def phantom(text: str, prompt: str, low_words: list[str]) -> bool:
    """Observed Sun 02:55: on SILENCE or NOISE the model returns our own hint ("context: ### The speaker mixes…") or a
    single hint word ("complaint", "email") with low confidence. Real speech is kept — even short commands like
    "call Mummy" — because clearly spoken words come back with high confidence."""
    if LEAK.search(text):
        return True
    words = [w.lower() for w in re.findall(r"[\wఀ-౿ऀ-ॿ]+", text)]
    hint = {w.lower() for w in re.findall(r"[\wఀ-౿ऀ-ॿ]+", prompt)}
    return bool(words) and all(w in hint for w in words) and bool(low_words)


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
    lps = [getattr(t, "logprob", 0) for t in getattr(r, "logprobs", None) or []]
    mean_lp = sum(lps) / len(lps) if lps else 0.0
    # Measured Sun 03:00: real speech mean logprob −0.09…−0.17; silence/noise phantoms −0.87…−2.73
    # (one was "మమ్మీకి call చేయు" from pure silence). Below −0.7 = not trusted as speech.
    if mean_lp < -0.7 or phantom(text, vocabulary(user), low_words):
        store().add_event("-", "heard_rejected", {"text": text[:200], "source": source, "mean_logprob": round(mean_lp, 2)})
        return None  # caller says "I didn't hear anything — try again"
    heard = Heard(text=text, languages=_languages(text), low_confidence_spans=low_words,
                  model=s.openai_model_transcribe, audio_ref=audio_ref, source=source)
    store().add_event("-", "heard", heard.model_dump())
    return heard
