"""Render a SlideDeck (written by the brain) to .pptx. Number tables come from code, not the LLM."""
import io
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

INK, ACCENT, MUTED = RGBColor(0x1D, 0x1D, 0x1F), RGBColor(0x1F, 0x6F, 0x4D), RGBColor(0x6B, 0x6B, 0x70)


def _title(slide, text: str, size: int = 32) -> None:
    tb = slide.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(12), Inches(1))
    p = tb.text_frame.paragraphs[0]
    p.text, p.font.size, p.font.bold, p.font.color.rgb = text, Pt(size), True, INK


def _bullets(slide, items: list[str], top: float = 1.5) -> None:
    tb = slide.shapes.add_textbox(Inches(0.8), Inches(top), Inches(11.5), Inches(5))
    tf = tb.text_frame
    tf.word_wrap = True
    for i, b in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text, p.font.size, p.font.color.rgb = f"•  {b}", Pt(20), INK
        p.space_after = Pt(10)


def render(deck: dict[str, Any], table: list[tuple[str, str]] | None = None, total: str | None = None,
           footer: str = "") -> bytes:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    s = prs.slides.add_slide(blank)  # title slide
    _title(s, deck["title"], 40)
    sub = s.shapes.add_textbox(Inches(0.6), Inches(1.6), Inches(12), Inches(1)).text_frame.paragraphs[0]
    sub.text, sub.font.size, sub.font.color.rgb = deck.get("subtitle") or "", Pt(20), MUTED
    if total:
        big = s.shapes.add_textbox(Inches(0.6), Inches(3.0), Inches(12), Inches(1.5)).text_frame.paragraphs[0]
        big.text, big.font.size, big.font.bold, big.font.color.rgb = f"Total: ₹{total}", Pt(54), True, ACCENT
    for sl in deck.get("slides", []):
        s = prs.slides.add_slide(blank)
        _title(s, sl["title"])
        _bullets(s, sl.get("bullets", []))
    if table:
        s = prs.slides.add_slide(blank)
        _title(s, "Item-wise breakdown (as printed)")
        rows = len(table) + 1 + (1 if total else 0)
        t = s.shapes.add_table(rows, 2, Inches(0.8), Inches(1.5), Inches(11.5), Inches(0.4) * rows).table
        t.columns[0].width, t.columns[1].width = Inches(8.5), Inches(3)
        t.cell(0, 0).text, t.cell(0, 1).text = "Item", "Amount (₹)"
        for r, (label, amt) in enumerate(table, start=1):
            t.cell(r, 0).text, t.cell(r, 1).text = label, amt
        if total:
            t.cell(rows - 1, 0).text, t.cell(rows - 1, 1).text = "TOTAL (computed in code)", total
    if footer:
        for s in prs.slides:
            f = s.shapes.add_textbox(Inches(0.6), Inches(6.9), Inches(12), Inches(0.4)).text_frame.paragraphs[0]
            f.text, f.font.size, f.font.color.rgb = footer, Pt(11), MUTED
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
