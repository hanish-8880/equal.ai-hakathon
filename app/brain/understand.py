"""Listening and understanding pipeline (CLAUDE.md §3A): capture → transcribe → normalise → understand → gate → act.
One typed result; every stage logged."""
import io
import json
import re
from typing import Any

from app.brain.client import json_call, oai, prompt
from app.brain.normalise import Normalised, normalise
from app.brain.schemas import Heard, UnderstandResult
from app.config import settings
from app.store.dynamo import store

PLUMBING_VOCAB = ("angle valve, ball valve, pillar cock, bib cock, health faucet, flush tank, geyser, CPVC, UPVC, PVC, elbow, tee, "
                  "reducer, union, hose, connection pipe, teflon tape, half inch, three-fourth inch, 15 mm, 20 mm")
BRANDS = "Jaquar, Cera, Hindware, Parryware, Astral, Supreme, Ashirvad, Finolex, Prince"
CONSEQUENTIAL = {"book_visit", "request_approval", "close_job_invoice", "source_part", "set_dependency", "cancel"}
CONFIDENCE_MIN = 0.8


def vocabulary(user: str) -> str:
    """Domain terms + THIS plumber's contact/shop names, open job ids, work orders and recent model numbers."""
    from app.jobs import store as jobs
    names = sorted({c.get("alias", "").title() for c in store().scan("contacts", user=user) if c.get("alias")})
    open_ = jobs.open_jobs(user)
    extra = [f"Job {j.job_id[1:]}" for j in open_] + [j.roles.work_order for j in open_ if j.roles.work_order] \
        + [p.model for j in open_ for p in j.parts_needed if p.model]
    known = ", ".join(x for x in names + extra if x)
    return (f"Plumbing terms: {PLUMBING_VOCAB}. Brands: {BRANDS}. "
            + (f"Names, shops, jobs: {known}. " if known else "")
            + "The speaker is a plumber mixing Telugu, English and Hindi.")


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
    """Step 2. None for silent / empty audio (caller replies "I didn't hear anything, try again")."""
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
    # map low-probability token fragments to whole words of the transcript (what the plumber sees highlighted)
    words = re.findall(r"[\wఀ-౿‌-]+", text)
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


def _recent(user: str, n: int = 6) -> list[dict[str, str]]:
    ev = store().events(f"chat:{user}")[-n:]
    return [{"who": "plumber" if e["kind"] in ("chat_in", "chat_heard") else "chinku",
             "text": (e["payload"].get("text") or "")[:300]} for e in ev]


def understand(user: str, text: str, image: dict[str, Any] | None = None) -> tuple[Normalised, UnderstandResult]:
    """Steps 3-4: code normalisation, then the structured LLM router."""
    from app.jobs import store as jobs
    s = settings()
    norm = normalise(text or "")
    open_ = [{"job_id": j.job_id, "issue": j.issue.value, "site": j.flat.value or j.site_address.value, "status": j.status,
              "next_action": j.next_action.what if j.next_action else None} for j in jobs.open_jobs(user)]
    contacts = [{k: c.get(k) for k in ("alias", "phone", "email")} for c in store().scan("contacts", user=user) if c.get("alias")]
    payload = json.dumps({"normalised_text": norm.text, "original_text": text, "spans": [sp.model_dump() for sp in norm.spans],
                          "corrections": norm.corrections, "attachment": image, "open_jobs": open_, "contacts": contacts,
                          "recent_conversation": _recent(user)}, ensure_ascii=False, default=str)
    from app.jobs import prefs
    sysmsg = prompt("understand").replace("{plumber_name}", s.plumber_name).replace("{language}", prefs.language(user))
    res = json_call(s.openai_model_brain, sysmsg, payload,
                    UnderstandResult, None, "understand", effort="low")  # 3.7 s vs 14.6 s, same result (DECISIONS 20:45)
    store().add_event("-", "understood", {"normalised": norm.model_dump(), "result": res.model_dump()})
    return norm, res


def gate(res: UnderstandResult) -> str:
    """Confidence gate (§3A step 4): 'ask' (no state change) | 'confirm' (readback, wait for yes) | 'act'."""
    if not res.actions or res.clarifying_question:
        return "ask"
    for a in res.actions:
        if a.intent in ("small_talk", "other"):
            continue
        if a.confidence < CONFIDENCE_MIN or a.missing or a.job_ref == "ambiguous":
            return "ask"
    if any(a.intent in CONSEQUENTIAL for a in res.actions):
        return "confirm"
    return "act"
