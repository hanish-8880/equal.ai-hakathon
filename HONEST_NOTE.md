# HONEST NOTE — Chinku

**Built by me:** planner and tool engine, call state machine, approval gate, memory, reminders, prompts and schemas,
exact transcription + extraction logic, Telugu/English/Hindi number normaliser, translation guard, Chinku App UI, eval harness.
**Reused:** OpenAI (gpt-5.5 brain, vision, gpt-4o-transcribe, TTS, web search), OmniDimension (phone calls, voice),
Gmail SMTP/IMAP, AWS (EC2, DynamoDB via instance role), OpenStreetMap + Leaflet, Twilio WhatsApp sandbox (optional).

| Component | Status |
|---|---|
| Planner — one request → plan of tool steps; conversation memory (recent chat + earlier results); personal memory | LIVE (Sat 23:55 / Sun 00:40): J1 photo → exact transcript → email with the photo attached (after "yes") → reminder; follow-up "email the important points of that page to Suresh" resolved from memory and the earlier photo |
| Approval gate — every email and call waits for "yes"; a second "yes" never sends twice; "no" sends nothing | LIVE + tests (enforced in code even if the plan forgets) |
| Exact transcription (`transcribe_image`) | LIVE on 1 test page written in a handwriting FONT (SIMULATED input): 0 word errors. Real handwritten pages + WER eval PENDING |
| Memory (`remember / recall / forget`, view + delete in Settings) | LIVE. Refuses OTPs/passwords/PINs, card numbers, bank account numbers, government IDs (code rule, tested) |
| Reminders (one-time / daily / weekly, in app) | LIVE (set + shown on Home); firing tested in code, idempotent. Reminder CALLS not built |
| Email | Gmail SMTP. Demo safety ON: EMAIL_DEMO_OVERRIDE delivers every email to my own inbox (subject says who it would go to) |
| Languages — UI + every Chinku message in Telugu / Hindi / Telugu-English mix | LIVE. Numbers guarded in code (a translation that changes a number → English shown instead) |
| Chinku App (home "What do you need?", Needs you, Recent work, reminders, chat cards, map, 🔊 Listen, Settings) | LIVE at /app?key=… — hold-to-talk mic not yet tested on a real phone |
| Phone: outbound calls (call tool → proven call engine), inbound "tell Chinku what you need" | Agents renamed to Chinku (read back OK). Chinku call tool NOT yet verified live |
| Search (OpenAI web search), map card | Wired — NOT yet verified live |
| Connected services — **Kiwi.com flights** (MCP, no auth) | **LIVE** (Sun 01:30): real HYD→BLR fares in INR, ranked in code (cheapest/fastest/earliest), card with Kiwi's booking link. Chinku never pays — the user books on Kiwi.com |
| Connected services — **Swiggy Food + Instamart** (official Swiggy MCP, user's own login via OTP) | Login + read-only search **LIVE-tested** (real restaurants, real soap prices; per-100 g computed in code). Not yet in the app; cart/checkout never without the user's tap. Production access is "invite-based" per Swiggy docs — may change |
| Ola / Uber / Rapido, Zomato | NOT BUILT (rides planned as DEMO adapters, labelled) |
| Promises + follow-up, Connect me (transfer), language bridge, scam check | NOT BUILT |

**Simulated:** test page (handwriting font); businesses / support agents will be played by consenting people; shortened
follow-up timers in the demo (to be labelled); any replayed run.
**Limits:** no keypad/IVR navigation (OmniDimension has no DTMF option); one concurrent call on our plan; cannot act
inside other apps or log into accounts; WhatsApp sandbox only reaches joined numbers.
**Removed:** the earlier "Chinku for Plumbers" code was removed on Sun 00:30 (still in git history).
