"""Exact transcription eval (CLAUDE.md §12.1): Chinku's transcribe_image prompt vs a plain "read this image" prompt,
SAME model, on every eval/pages/<name>.jpg that has a typed <name>.txt ground truth.
Metrics: word error rate (WER) and hallucinated words (output words that never appear in the page).
Run: python -m eval.transcription_eval   → eval/transcription.json"""
import json
import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel

from app.brain.client import image_part, json_call, oai, prompt
from app.config import settings

PAGES = Path(__file__).parent / "pages"


class T(BaseModel):
    text: str = ""


def words(s: str) -> list[str]:
    s = unicodedata.normalize("NFC", s).lower().replace("₹", "rs ")
    return re.findall(r"[\wఀ-౿ऀ-ॿ]+(?:[.,:/-]\w+)*", s)


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[len(hyp)], len(ref)


def chinku(img: bytes) -> str:
    return json_call(settings().openai_model_brain, prompt("transcribe_image"),
                     [{"type": "text", "text": "Transcribe exactly."}, image_part(img, "image/jpeg")], T, None, "eval_transcribe").text


def baseline(img: bytes) -> str:
    r = oai().chat.completions.create(model=settings().openai_model_brain, messages=[
        {"role": "user", "content": [{"type": "text", "text": "Read this image."}, image_part(img, "image/jpeg")]}])
    return r.choices[0].message.content or ""


def main() -> None:
    rows = []
    for txt in sorted(PAGES.glob("*.txt")):
        jpg = txt.with_suffix(".jpg")
        if not jpg.exists():
            continue
        truth, img = txt.read_text(encoding="utf-8"), jpg.read_bytes()
        ref = words(truth)
        row = {"page": txt.stem, "kind": "simulated (font)", "truth_words": len(ref)}
        for name, fn in (("chinku", chinku), ("baseline", baseline)):
            out = fn(img)
            hyp = [w for w in words(out) if w != "unclear"]
            errs, n = wer(ref, hyp)
            extra = [w for w in hyp if w not in set(ref)]
            row[name] = {"wer": round(errs / max(1, n), 3), "errors": errs, "hallucinated": len(extra), "hallucinated_words": extra[:12],
                         "unclear_marks": out.count("[unclear]"), "output": out[:600]}
        rows.append(row)
        print(f"{row['page']:<14} chinku WER {row['chinku']['wer']:.3f} (+{row['chinku']['hallucinated']})   "
              f"baseline WER {row['baseline']['wer']:.3f} (+{row['baseline']['hallucinated']})", flush=True)
    (Path(__file__).parent / "transcription.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
