"""Generate a MOCK property banner (SIMULATED input) with a consenting volunteer's number.
Run: python -m scripts.make_banner <10-digit phone> [out.png]
"""
import base64
import sys
from pathlib import Path

from app.brain.client import oai

phone = sys.argv[1]
out = Path(sys.argv[2] if len(sys.argv) > 2 else "media_tmp/banner_demo.png")
pretty = f"{phone[:5]} {phone[5:]}"
prompt = (
    "Photo of a large outdoor real-estate flex banner tied to a fence on a Hyderabad roadside, slight angle, daylight. "
    "Banner text, large and crisp, exactly as written: "
    "'GREEN VALLEY RESIDENCY' (headline), '2 & 3 BHK Luxury Apartments', 'Kollur, Hyderabad', "
    "'2BHK starting ₹65 Lakhs*', 'Site visits open', and a big phone line 'Call: " + pretty + "'. "
    "Fictional project, no real brand logos."
)
r = oai().images.generate(model="gpt-image-2", prompt=prompt, size="1024x1536")
out.parent.mkdir(exist_ok=True)
out.write_bytes(base64.b64decode(r.data[0].b64_json))
print("saved", out, out.stat().st_size, "bytes")
