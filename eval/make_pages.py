"""Make 12 test pages with known text (handwriting-style FONTS, rotation, noise, blur) — SIMULATED inputs for the
transcription eval. Real handwritten pages can be added to eval/pages/ as <name>.jpg + <name>.txt and are picked up too.
Run: python -m eval.make_pages"""
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "pages"
F = "C:/Windows/Fonts/"
PAGES = {
    "p01_shopping": ("Inkfree.ttf", ["Shopping - Saturday", "Rice 5 kg", "Toor dal 2 kg", "Milk 3 packets", "Curd 500 g", "Onions 2 kg - Rs 70"]),
    "p02_meds": ("segoesc.ttf", ["Amma medicines", "BP tablet 5 mg - morning", "Sugar tablet - after lunch", "Calcium - night", "Doctor visit 28 Nov 6:30 pm"]),
    "p03_bills": ("segoepr.ttf", ["Bills this month", "Electricity Rs 1,840 due 28 Nov", "Water Rs 320", "Internet Rs 799 auto-debit 5th", "Gas cylinder Rs 905"]),
    "p04_class": ("LHANDW.TTF", ["Tuition notes 21/11", "Chapter 4 Photosynthesis", "Test on Monday 9 am", "Bring geometry box", "Fee Rs 1,500 by 30th"]),
    "p05_contacts": ("comic.ttf", ["Important numbers", "Ravi plumber 98480 22222", "Sai electrician 99490 11111", "Society office 040-2345 6789"]),
    "p06_trip": ("BRADHITC.TTF", ["Araku trip plan", "Leave 5:30 am Saturday", "Borra caves 10 am", "Coffee museum 3 pm", "Budget Rs 15,000 for 4"]),
    "p07_meeting": ("mvboli.ttf", ["Meeting with Suresh", "Discuss invoice INV-2207", "Ask for 15 percent advance", "Send quote by Tuesday"]),
    "p08_recipe": ("segoescb.ttf", ["Tomato rasam", "4 tomatoes", "1 tsp pepper", "2 cups water", "Tamarind small ball", "Boil 10 minutes"]),
    "p09_te_list": ("Nirmala.ttc", ["సరుకులు", "బియ్యం 5 కిలోలు", "పాలు 2 ప్యాకెట్లు", "ఉల్లిపాయలు 1 కిలో", "కూరగాయలు 200 రూపాయలు"]),
    "p10_te_note": ("Nirmala.ttc", ["అమ్మకు గుర్తు", "రేపు ఉదయం 9 గంటలకు డాక్టర్", "BP మాత్ర మర్చిపోవద్దు", "ఫోన్ 98480 22222"]),
    "p11_mixed": ("Inkfree.ttf", ["To do (Sunday)", "Call Kiran re: car service", "Pay LIC premium Rs 12,450", "Book Tatkal ticket 10 am", "Gym 6 pm"]),
    "p12_receipt": ("comic.ttf", ["Sri Balaji Kirana", "Bill no 4471   22-11-2026", "Sunflower oil 1 L   Rs 155", "Sugar 1 kg   Rs 46", "Total Rs 201"]),
}


def make(name: str, font: str, lines: list[str], seed: int) -> None:
    rnd = random.Random(seed)
    img = Image.new("RGB", (1100, 140 + 105 * len(lines)), (250, 247, 236))
    d = ImageDraw.Draw(img)
    for y in range(110, img.height, 52):
        d.line([(40, y), (1060, y)], fill=(195, 208, 232), width=2)
    f = ImageFont.truetype(F + font, 46)
    for i, t in enumerate(lines):
        d.text((90 + rnd.randint(-8, 8), 60 + i * 105 + rnd.randint(-6, 6)), t, font=f, fill=(25 + rnd.randint(0, 30), 40, 110))
    for _ in range(900):  # paper noise
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        d.point((x, y), fill=(200, 195, 180))
    img = img.rotate(rnd.uniform(-2.5, 2.5), expand=False, fillcolor=(250, 247, 236)).filter(ImageFilter.GaussianBlur(0.6))
    img.save(OUT / f"{name}.jpg", quality=80)
    (OUT / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_browser(name: str, lines: list[str], seed: int) -> None:
    """Telugu needs proper letter joining (PIL here has no raqm) → let Chrome draw it, then add the same noise/rotation."""
    import html
    import subprocess
    import tempfile
    rnd = random.Random(seed)
    h = 140 + 105 * len(lines)
    body = "".join(f'<div style="margin-left:{90 + rnd.randint(-8, 8)}px;height:105px;line-height:105px">{html.escape(t)}</div>' for t in lines)
    page = (f'<html><body style="margin:0;width:1100px;height:{h}px;background:#FAF7EC;color:#1E2A6E;'
            f'font:46px Nirmala UI, Gautami, sans-serif;padding-top:20px">{body}</body></html>')
    with tempfile.TemporaryDirectory() as td:
        src, png = Path(td) / "p.html", Path(td) / "p.png"
        src.write_text(page, encoding="utf-8")
        subprocess.run([r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--window-size=1100,{h}", f"--screenshot={png}", src.as_uri()], check=True, capture_output=True, timeout=60)
        img = Image.open(png).convert("RGB")
    d = ImageDraw.Draw(img)
    for _ in range(900):
        d.point((rnd.randrange(img.width), rnd.randrange(img.height)), fill=(200, 195, 180))
    img = img.rotate(rnd.uniform(-2.5, 2.5), expand=False, fillcolor=(250, 247, 236)).filter(ImageFilter.GaussianBlur(0.6))
    img.save(OUT / f"{name}.jpg", quality=80)
    (OUT / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for i, (n, (font, lines)) in enumerate(PAGES.items()):
        (make_browser(n, lines, i) if font == "Nirmala.ttc" else make(n, font, lines, i))
    print(f"{len(PAGES)} pages -> {OUT}")
