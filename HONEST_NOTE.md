# HONEST NOTE — Chinku

**Built by me (Hanish, with Claude Code):** planner and tool engine, call state machine, approval gate (in code), memory,
reminders and reminder calls, promises + follow-up + escalation, connected-services adapter layer and ranking engine,
prompts and schemas, exact transcription + extraction logic, Telugu/English/Hindi number normaliser, translation guard,
Chinku App UI, eval harness.
**Reused:** OpenAI (gpt-5.5 planning/vision/extraction, gpt-4o-transcribe, TTS, web search), OmniDimension (phone calls,
voice agent), Kiwi.com MCP (flights), Swiggy MCP (Instamart, the user's own login), Gmail SMTP/IMAP, AWS (EC2, DynamoDB),
OpenStreetMap + Leaflet, Twilio WhatsApp sandbox (optional, not in the demo).

| Component | Status (observed) |
|---|---|
| Planner (one request → steps), conversation + personal memory | **LIVE** — 22/22 eval requests planned correctly, 0 wrong-action plans (builder-written requests) |
| Approval gate — email, call, Swiggy cart, reminder call | **LIVE** + tests; enforced in code even if the plan forgets; second "yes" never repeats |
| J1 photo → exact transcript → email → reminder | **LIVE** — email arrived word for word with the photo attached (IMAP-verified) |
| Exact transcription | **LIVE**; eval on 13 **SIMULATED** font pages (0.8% WER vs 17.5% baseline). Real handwriting NOT yet measured |
| J3 complaint: official support → email / call → promise → follow-up → escalation | Official contact **LIVE** (Bajaj, code-checked domain); email drafted, waiting for approval; promise/follow-up/escalation tested in code, NOT yet run on a real support call |
| J2 doctor booking | Clinic search **LIVE** (6 real clinics, sources); map pins are approximate (locality) because OpenStreetMap can't find full Indian addresses; clinic + Mummy calls NOT yet run live |
| J4 photo → call | **LIVE** to the approval card; the call itself waits for a consenting test (owner's own number) |
| J7 scam check | **LIVE** — real scam SMS → Likely scam with reasons + official sources; code red flags can't be overridden to "genuine" |
| J9 flights — Kiwi.com | **LIVE** — real HYD→BLR fares in INR, ranked in code, booking link opens Kiwi; Chinku never pays |
| J9 groceries — Swiggy Instamart | **LIVE** — real products/prices, recipe → ingredients → packs chosen in code; adds to the user's cart only after approval; checkout/payment/clear-cart blocked in code. Production access is "invite-based" per Swiggy docs — it may be switched off |
| J9 rides — Ola, Uber, Rapido | **DEMO data** (real road distance, demo fares/ETAs, labelled "Demo data"); real "open in app" links for Uber/Ola |
| J9 Swiggy Food | **LIVE** — real dishes, filters/ranking in code, added to the real food cart after approval, real bill (item + delivery + taxes = to pay) and closed/out-of-stock warnings. Chinku never places the order or pays |
| J9 Swiggy Dineout | **LIVE** search + slot check (real FREE/PAID slots); FREE table booking built behind approval — NOT yet booked live (real reservation; user's tap) |
| Zomato | NOT BUILT (not permitted for third-party apps) |
| J10 voice-first (tap to talk, silence auto-stop, Talk mode, replies spoken) | Built; checked in a desktop browser; NOT yet tested on a real phone |
| J11 trip planner (itinerary + rated hotels/restaurants → pick → calls) | **LIVE** search/ranking; the hotel calls NOT yet run (real hotels are only called with consent, never at night) |
| J5 "Connect me now" live transfer | NOT BUILT — fallback "Talk to them now" one-tap call card |
| Toll-free / landline calling (complaints) | Built + tested in code (number kinds, night rule exempts 1800 helplines). Live: Bajaj's official number is a landline → correctly not called at 1 AM. Real helpline call NOT yet made; IVR keypad menus are a known limit |
| Chinku App — ChatGPT-style chat; approvals (Call now / Send / Add to cart / Book table) are buttons in the chat; drawer for Needs you, reminders, recent work, memory | **LIVE** at /app?key=… (screenshots checked at 390×844); voice mic NOT yet tested on a real phone |
| J6 language bridge | Kannada + Tamil enabled on the voice agent (settings read back); a Kannada call is NOT yet verified |
| J8 group ask | NOT BUILT (the call engine can call several people one after another) |
| Languages (UI + every message in te / hi / te-en) | **LIVE** — translation that changes a number is rejected → English shown |
| Persistence | **LIVE** — server restarted mid-task, same pending approval still there (3.2 s) |
| Email | Gmail SMTP. Demo safety: EMAIL_DEMO_OVERRIDE delivers every email to my own inbox (subject names the real recipient) |

**Simulated:** eval pages (handwriting fonts), the visiting card and the to-do page, businesses and support agents in the
demo (played by consenting people), shortened follow-up timers (labelled "Demo: shortened"), ride fares/ETAs (demo data),
any replayed run.
**Live regression:** 17/18 features passed on real services (eval/results.md §5); the miss was Swiggy rate-limiting our account (HTTP 429) after many test runs — Chinku now caches lookups and backs off.
**Limits:** Swiggy may rate-limit (429) under heavy use; no keypad/IVR menus (OmniDimension has no DTMF); one concurrent call on our plan; cannot log into other apps or
pay; ride/food platforms without an API are demo data; map pins can be approximate; WhatsApp sandbox only reaches joined
numbers.
**Removed:** the earlier "Chinku for Plumbers" code (Sun 00:30) — still in git history.
