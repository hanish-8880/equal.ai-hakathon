"""Step 3 of the listening pipeline (CLAUDE.md §3A): deterministic normalisation, code first.
Spoken numbers in Telugu (script + romanized), English and Hindi → digits; money/units; job refs; phones; model codes;
self-corrections. Every number and model code found is returned as a span with check=True so the router asks or
reads back before acting on it."""
import re
from typing import Any

from pydantic import BaseModel, Field

# value, kind: "unit" (0-9), "teen" (10-19), "tens" (20-90), "hundred", "thousand", "lakh", "crore"
_W: dict[str, tuple[int, str]] = {}


def _add(kind: str, value: int, *words: str) -> None:
    for w in words:
        _W[w.lower()] = (value, kind)


# English
for i, w in enumerate("zero one two three four five six seven eight nine".split()):
    _add("unit", i, w)
for i, w in enumerate("ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()):
    _add("teen", 10 + i, w)
for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split()):
    _add("tens", 20 + 10 * i, w)
_add("hundred", 100, "hundred", "hundreds")
_add("thousand", 1000, "thousand", "thousands")
_add("lakh", 100000, "lakh", "lakhs", "lac", "lacs")
_add("crore", 10000000, "crore", "crores")
# Telugu romanized (common spellings)
_add("unit", 1, "okati", "oka", "okka"); _add("unit", 2, "rendu", "renDu"); _add("unit", 3, "moodu", "mudu", "mooDu")
_add("unit", 4, "nalugu", "naalugu"); _add("unit", 5, "aidu", "ayidu", "aydu"); _add("unit", 6, "aaru", "aru")
_add("unit", 7, "edu", "eedu"); _add("unit", 8, "enimidi", "enmidi"); _add("unit", 9, "tommidi", "thommidi")
_add("teen", 10, "padi", "padhi"); _add("teen", 11, "padakondu", "padhakondu"); _add("teen", 12, "pannendu")
_add("teen", 13, "padamudu", "padhamudu"); _add("teen", 14, "padnalugu", "padhnalugu"); _add("teen", 15, "padihenu", "padhihenu", "padhinaidu")
_add("teen", 16, "padaharu", "padahaaru"); _add("teen", 17, "padihedu", "padhihedu"); _add("teen", 18, "paddenimidi", "padhenimidi", "paddhenimidi")
_add("teen", 19, "pandommidi", "pandhommidi")
_add("tens", 20, "iravai", "iruvai"); _add("tens", 30, "muppai"); _add("tens", 40, "nalabhai", "nalabai", "nalubhai")
_add("tens", 50, "yabhai", "yabai", "yaabhai", "yaabai"); _add("tens", 60, "aravai"); _add("tens", 70, "debbai")
_add("tens", 80, "enabhai", "enabai"); _add("tens", 90, "tombhai", "tombai", "thombhai")
_add("hundred", 100, "vanda", "vandala", "vandalu", "vandha"); _add("thousand", 1000, "veyyi", "velu", "vela")
_add("lakh", 100000, "laksha", "lakshala", "lakshalu")
# Telugu script
_add("unit", 1, "ఒకటి", "ఒక", "ఒక్క"); _add("unit", 2, "రెండు"); _add("unit", 3, "మూడు"); _add("unit", 4, "నాలుగు")
_add("unit", 5, "ఐదు", "అయిదు"); _add("unit", 6, "ఆరు"); _add("unit", 7, "ఏడు"); _add("unit", 8, "ఎనిమిది"); _add("unit", 9, "తొమ్మిది")
_add("teen", 10, "పది"); _add("teen", 11, "పదకొండు"); _add("teen", 12, "పన్నెండు"); _add("teen", 13, "పదమూడు")
_add("teen", 14, "పధ్నాలుగు", "పద్నాలుగు"); _add("teen", 15, "పదిహేను", "పదిహేన్"); _add("teen", 16, "పదహారు")
_add("teen", 17, "పదిహేడు"); _add("teen", 18, "పద్దెనిమిది"); _add("teen", 19, "పందొమ్మిది")
_add("tens", 20, "ఇరవై"); _add("tens", 30, "ముప్పై", "ముప్పది"); _add("tens", 40, "నలభై", "నలబై"); _add("tens", 50, "యాభై", "యాబై")
_add("tens", 60, "అరవై"); _add("tens", 70, "డెబ్బై"); _add("tens", 80, "ఎనభై", "ఎనబై"); _add("tens", 90, "తొంభై", "తొంబై")
_add("hundred", 100, "వంద", "వందల", "వందలు"); _add("thousand", 1000, "వెయ్యి", "వేల", "వేలు")
_add("lakh", 100000, "లక్ష", "లక్షల", "లక్షలు"); _add("crore", 10000000, "కోటి", "కోట్ల")
# Telugu-script English loanwords (gpt-transcribe writes these)
for en, te in [("one", "వన్"), ("two", "టూ"), ("three", "త్రీ"), ("four", "ఫోర్"), ("five", "ఫైవ్"), ("six", "సిక్స్"), ("seven", "సెవెన్"),
               ("eight", "ఎయిట్"), ("nine", "నైన్"), ("ten", "టెన్"), ("twenty", "ట్వెంటీ"), ("thirty", "థర్టీ"), ("fifty", "ఫిఫ్టీ"),
               ("hundred", "హండ్రెడ్")]:
    _W[te] = _W[en]
# Hindi (romanized)
_add("unit", 1, "ek"); _add("unit", 2, "do"); _add("unit", 3, "teen"); _add("unit", 4, "char", "chaar"); _add("unit", 5, "paanch", "panch")
_add("unit", 6, "chhe", "che"); _add("unit", 7, "saat"); _add("unit", 8, "aath"); _add("unit", 9, "nau"); _add("teen", 10, "das")
_add("tens", 20, "bees"); _add("tens", 50, "pachaas", "pachas"); _add("hundred", 100, "sau"); _add("thousand", 1000, "hazaar", "hazar")

# short words that are also everyday English/Telugu words: numbers ONLY next to another number word or a unit
AMBIGUOUS = {"do", "che", "char", "das", "teen", "aru", "edu", "eedu", "oka", "ek", "nau", "sau", "padi", "aidu"}
UNIT_WORDS = {"rupees", "rs", "piece", "pieces", "pcs", "each", "bags", "valves", "valve", "inch", "mm", "units", "nos"}

CORRECTION = re.compile(r"\b(no|not|nahi|nahin|kaadu|kadu|sorry|i mean|actually|wait)\b|కాదు|కాదండి|సారీ", re.I)
UNIT_AFTER = re.compile(r"^\s*(?:/-)?\s*(per piece|each|a piece|per unit|per bag|per meter|per metre|total|inch(?:es)?|mm|ft|feet|meters?|"
                        r"rupees|rs\.?|₹|pieces?|pcs|nos|units?)", re.I)
MODEL_CODE = re.compile(r"\b(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b|\b[A-Z]{2,}\d{2,}[A-Z0-9]*\b")
JOB_REF = re.compile(r"(?:\bjob\s*(?:no\.?|number)?\s*|\bJ|జాబ్\s*)(\d{1,4})\b", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")


class Span(BaseModel):
    raw: str
    kind: str  # number | money | quantity | job_ref | phone | model_code
    value: Any
    unit: str | None = None
    check: bool = True  # numbers and model codes are always confirmed (readback) before acting


class Normalised(BaseModel):
    text: str  # transcript with spoken numbers replaced by digits
    spans: list[Span] = Field(default_factory=list)
    corrections: list[dict[str, Any]] = Field(default_factory=list)


def _tokens(s: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)*|[\wఀ-౿]+|[^\s\w]", s)


def words_to_number(words: list[str]) -> int | None:
    """Indian composition: [units/teens/tens]* with hundred/thousand/lakh/crore multipliers.
    Colloquial price shorthand: 'two fifty' = 250, 'three fifty' = 350 (unit followed directly by tens)."""
    vals = [_W.get(w.lower()) for w in words]
    if not vals or any(v is None for v in vals):
        return None
    if len(vals) == 2 and vals[0][1] == "unit" and vals[1][1] == "tens":  # "two fifty"
        return vals[0][0] * 100 + vals[1][0]
    total, cur = 0, 0
    for v, kind in vals:
        if kind in ("unit", "teen", "tens"):
            cur += v
        elif kind == "hundred":
            cur = (cur or 1) * 100
        else:  # thousand / lakh / crore
            total += (cur or 1) * v
            cur = 0
    return total + cur


def normalise(text: str) -> Normalised:
    out: list[str] = []
    spans: list[Span] = []
    # protect model codes (ALD-0517) so their digits are never read as numbers; restored below exactly as written
    codes = [m.group(0) for m in MODEL_CODE.finditer(text) if not PHONE.fullmatch(m.group(0))]
    protected = text
    for k, c in enumerate(codes):
        protected = protected.replace(c, f"MODELCODE{k}X", 1)
    toks = _tokens(protected)
    i = 0
    while i < len(toks):
        t = toks[i]
        # numeric literal, possibly with decimal + lakh/crore ("1.5 lakh")
        if re.fullmatch(r"\d+(?:[.,]\d+)*", t):
            num = float(t.replace(",", "")) if "." in t else int(t.replace(",", ""))
            j = i + 1
            if j < len(toks) and _W.get(toks[j].lower(), (0, ""))[1] in ("lakh", "crore", "thousand"):
                num = num * _W[toks[j].lower()][0]
                j += 1
            num = int(num) if float(num).is_integer() else num
            raw = " ".join(toks[i:j])
            out.append(str(num)); spans.append(Span(raw=raw, kind="number", value=num)); i = j
            continue
        if t.lower() in _W:
            j = i
            while j < len(toks) and toks[j].lower() in _W:
                j += 1
            # longest prefix that parses
            while j > i and words_to_number(toks[i:j]) is None:
                j -= 1
            n = words_to_number(toks[i:j]) if j > i else None
            if n is not None and j - i == 1 and t.lower() in AMBIGUOUS:
                nxt = toks[j].lower() if j < len(toks) else ""
                if nxt not in UNIT_WORDS and not nxt.isdigit():
                    n = None  # "what do you need" stays text
            if n is not None and not (j - i == 1 and _W[t.lower()][1] in ("hundred", "thousand", "lakh", "crore")):
                raw = " ".join(toks[i:j])
                out.append(str(n)); spans.append(Span(raw=raw, kind="number", value=n)); i = j
                continue
        out.append(t)
        i += 1
    norm = re.sub(r"\s+([.,!?;:])", r"\1", " ".join(out))
    norm = re.sub(r"(₹|Rs\.?)\s+(\d)", r"\1\2", norm)
    for k, c in enumerate(codes):
        norm = norm.replace(f"MODELCODE{k}X", c)
    # units / money for each number span (look at text after the number in the normalised string)
    pos = 0
    for sp in spans:
        k = norm.find(str(sp.value), pos)
        if k < 0:
            continue
        pos = k + len(str(sp.value))
        before = norm[max(0, k - 4):k]
        m = UNIT_AFTER.match(norm[pos:])
        if m:
            u = m.group(1).lower()
            sp.unit = "INR" if u in ("rupees", "rs", "rs.", "₹") else u
        if "₹" in before or "rs" in before.lower() or sp.unit == "INR":
            sp.kind = "money"
    # self-corrections: "350 … no, 300" → keep the last value, record the correction
    corrections = []
    nums = [s for s in spans if s.kind in ("number", "money")]
    for a, b in zip(nums, nums[1:]):
        ia, ib = norm.find(str(a.value)), norm.find(str(b.value), norm.find(str(a.value)) + 1)
        if 0 <= ia < ib and CORRECTION.search(norm[ia + len(str(a.value)):ib]) and ib - ia < 40:
            corrections.append({"from": a.value, "to": b.value, "raw": norm[ia:ib + len(str(b.value))]})
    # entities
    for m in JOB_REF.finditer(norm):
        spans.append(Span(raw=m.group(0), kind="job_ref", value=f"J{int(m.group(1))}", check=False))
    for m in PHONE.finditer(norm):
        digits = re.sub(r"\D", "", m.group(0))[-10:]
        spans.append(Span(raw=m.group(0), kind="phone", value="+91" + digits))
    for m in MODEL_CODE.finditer(text):  # from the ORIGINAL text: never "correct" model codes
        if not PHONE.fullmatch(m.group(0)) and not JOB_REF.fullmatch(m.group(0)):
            spans.append(Span(raw=m.group(0), kind="model_code", value=m.group(0)))
    return Normalised(text=norm, spans=spans, corrections=corrections)
