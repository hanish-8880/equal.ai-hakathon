"""Live check: translate the ₹1,400 readback into each language (run on the server)."""
from app.brain import translate

SRC = ("J31: 2 angle valve ₹700, 1 hose ₹300, labour ₹400. Total ₹1,400. Not billed: 1 × angle valve (returned / unused).\n"
       "Send to Sai Facility against WO-117? (yes / correct it)")
for c in ("te", "hi", "te-en"):
    out = translate.to_lang(SRC, c)
    print("==", c, "| numbers guard OK" if out != SRC else "| FELL BACK TO ENGLISH")
    print(out, "\n")
