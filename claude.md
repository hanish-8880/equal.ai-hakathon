# CLAUDE.md — Chinku · Equal AI Builders Hackathon 2026 (FINAL)

You are my senior product, design and engineering partner. I am **Hanish**, building solo at the
Equal AI × OpenAI Builders Hackathon, Equal HQ Hyderabad.
**Now: Sat 21 Nov, ~11:15 PM IST. Code freeze: Sun 22 Nov, 2:00 PM IST. Push + deploy by 12:00 PM.**

This file replaces every earlier CLAUDE.md (Chinku errands, Chinku for Plumbers, Instint drafts). The product name is **Chinku**.
It is the single source of truth. The existing Chinku repo is the foundation: **reuse it, never rewrite it.**

---

## 0. How you work with me

1. **Reuse first.** Inspect the repo, list what already works per tool (section 4), then extend.
2. **Build in increments:** implement → run → observe → report. Never report an unobserved result.
3. **Verify every external behaviour** (API, webhook payload, model name) with the smallest real test before
   depending on it. Record it in `DECISIONS.md` (Observed / Chose / Gave up / Would change if).
4. **Label everything** LIVE / SIMULATED / NOT BUILT. Keep `HONEST_NOTE.md` current.
5. **Deploy after every verified increment:** `git push` + `python -m scripts.aws_deploy push`.
6. **Protect the demo.** Follow the checkpoints and cut order (section 13). Never break a working journey to add one.
7. **No per-feature hacks in the engine.** New behaviour = a new tool, a planner rule, or a recipe.
8. **Errors are never silent.** The user always sees a plain-words status.
9. **Be concise.** Short reports, next concrete action.

---

## 1. Organizer requirements (fixed — do not change)

Solo build. Equal AI reviews every build, shortlists 10, those 10 demo live. Submission must include:
1. A working demo that runs end to end on fresh input (not slides or mock-ups).
2. Source code in a repository judges can open.
3. Evidence of results (accuracy vs a baseline, outputs on new data, proof data persists).
4. A short honest note: built by me / reused / simulated. Use only data we have permission to use.

Track: **Build Creative** — personal context (camera, mail, anything personal) into an everyday AI product;
show a working journey from raw input to a useful outcome.

---

## 2. Product

**Chinku: tell it what you need — type, speak in your language, or show a photo — and it finds the answer or
does the next step for you: searches, analyses, calls anyone, emails anyone, reminds you, and follows up until
it's done.**

### Why it wins (one step above Equal)
| Equal AI today | Chinku |
|---|---|
| Screens and answers calls coming **to** you | **Makes calls for you** — book, ask, complain, confirm |
| Voice only | **Photo, screenshot, PDF, voice or text** turned into action |
| Summary after a call | **Follow-through**: tracks promises ("callback by 4 PM"), reminds, calls again, escalates |
| One call | **A whole task** from one request: search → call → email → remind |
| — | **Connect me now**: waits on the line, transfers to you when a human picks up |
| — | **Language bridge**: calls in *their* language, reports in *yours* |

Equal has said users want the assistant to "act autonomously on behalf of the user — e.g. sending addresses to the
delivery person with consent or making outbound calls to book meetings." Chinku is that next step, built today.

**Pitch line:** *"Equal answers the calls that come to you. Chinku makes the calls, sends the mails, and follows up
until it's done."*

**Research to cite:** Indians spent 15+ billion hours on hold to customer service in a year — ~30.7 hours per person
(ServiceNow CX report). Google's call-for-you features (Hold for Me, Gemini Call for Me) are not available in India and
are English-only.

---

## 3. Stack and ownership (verified facts marked ✓)

| Layer | Owner | Facts |
|---|---|---|
| Outbound/inbound phone calls, voice | OmniDimension | ✓ One generic outbound agent `chinku-caller` (#286768) + `chinku-inbound` (#286801). ✓ Per-call brief via `call_context` reaches the prompt. ✓ Post-call webhook to AWS. Languages: English (India), Hindi, Telugu. LLM gpt-4.1, TTS Cartesia, STT Soniox. |
| | | ✓ **Call transfer available**: Call Configuration → Transfer & Routing → "Enable Call Transfer" and "Custom API transfer" (server returns `__omni_transfer_number`). Currently OFF — enable and test. |
| | | ✗ **No keypad/DTMF** option found → cannot navigate "press 1" menus. Label as NEXT. |
| | | Available in-call options: Web Search, Google Calendar, Cal.com, Custom API (during call), WhatsApp Cloud (Meta). Optional. |
| All intelligence | OpenAI | Transcribe, vision, planning, call briefs, extraction with evidence, replies, TTS, web search (verify availability on our credits). Strong model for extraction (cheap model misread a Telugu number: 885 vs 390). |
| Exactness | Code | Arithmetic, dates/times (IST), comparisons, validation, approvals, idempotency |
| Email | SMTP (Gmail app password) | ✓ works |
| State, schedule, hosting | AWS | ✓ EC2 us-west-2, stable https (sslip.io), ✓ DynamoDB via instance role, scheduler |
| Optional channel | Twilio WhatsApp sandbox | Unreliable (URL flips back; only joined numbers). **Not part of the demo.** |
| Maps | OpenStreetMap + Leaflet | Free, no key. Geocoding via Nominatim (respect usage policy) or OpenAI web search results. |

---

## 4. Tools (the planner composes these — fixed set)

| Tool | Does | Status |
|---|---|---|
| `analyze` | Photo/PDF/screenshot → structured content + type classification (handwritten page, bill, receipt, broken product, ticket screenshot, poster/banner/visiting card, notice, letter, suspicious SMS) | exists → extend classification |
| `transcribe_image` | Exact editable text; keep lines/numbers; `[unclear]` instead of guessing | NEW mode of analyze |
| `transcribe_audio` | Voice → text, Telugu/English/Hindi mixed, vocabulary prompt | exists |
| `extract` | Key facts (dates, amounts, names, IDs, phone numbers, deadlines) each with source quote | exists |
| `search` | Web research with sources + date checked (official support emails/helplines, hospitals/clinics, shops, facts) | NEW — verify OpenAI web search; else labelled NOT BUILT |
| `map` | Show places from `search` on an OpenStreetMap card; user picks one | NEW |
| `call` | Outbound call via OmniDimension with an OpenAI-written brief; extract answers with evidence quotes + status | exists |
| `connect_me` | During/after a call, transfer to the user's (or a named contact's) phone via Custom API transfer | NEW — verify |
| `draft_email` / `send_email` | Draft anything (text, transcript, complaint, attachment) to anyone; send after approval | exists |
| `remind` | One-time, repeating, or auto-created from a promise; delivered in app (+ optional call) | exists → add repeating + promise-based |
| `follow_up` | At a promise time: ask "Did they…?" and offer the next step (call again / escalate) | NEW (schedule + call) |
| `remember` / `recall` | Personal memory: contacts, places, preferences, accounts' non-secret identifiers, past tickets | NEW |
| `compute` | Totals, comparisons, dates — deterministic code | exists |
| `scam_check` | Analyze suspicious SMS/screenshot/number + search official sources → Likely scam / Looks genuine / Unsure, with reasons and sources | NEW (analyze + search) |

Existing recipes stay working and selectable by the planner (not in the headline): dealer quotes, banner rate,
warranty complaint, passbook explanation, appointment + reminder, invoice total.

---

## 5. Planner (core of Chinku)

Pipeline for every request: **capture → transcribe (voice) → analyze (attachments) → recall memory → plan →
approval → execute → results → follow-through.**

`plan(request, attachments, memory, conversation, active_tasks) -> Plan` (OpenAI, structured output, Pydantic):
```
Plan:
  understood: str                 # one-line readback, user's language
  steps: [ {id, tool, inputs{}, depends_on[], needs_approval: bool, output_name} ]
  clarifying_question: str|null   # ONE question when essentials are missing
  suggested_actions: [ {label, instruction} ]   # only when no instruction given (section 7)
  cannot_do: [ {what, why, best_alternative} ] # honest limits + next best step
  confidence: 0-1
```
Rules:
1. **One sentence → many steps.** "Transcribe this, email me a copy, remind me tomorrow at 9" =
   transcribe_image → draft_email(to=me) [approval] → send_email → remind(tomorrow 09:00 IST).
2. **The instruction decides what happens to an attachment.** "Transcribe" = exact text only. "Email the important
   details" = extract + short email. Never mix modes.
3. **Conversation memory.** "Call the second one", "same hospital", "email that to Suresh" resolve from the thread
   and memory. Never ask the user to repeat something already said.
4. **Try its best.** If a step is impossible (needs the user's app login, payment, OTP, IVR keypad), say exactly
   why in `cannot_do` and do the best alternative (call the helpline instead, show the official link, draft the text).
5. **Approval gate** (`needs_approval=true`): any call, any email/message to anyone, transfers, anything involving money,
   calling a number read from a photo. Reading, transcribing, searching, drafting, reminders to the user: no approval.
6. **Confidence < 0.8 or missing essentials** → one clarifying question, no action.
7. **Times** resolved in code (IST), always shown in words: "Tomorrow, Sun 22 Nov, 9:00 AM".
8. Every step output becomes a stored **Result** (section 8). Partial success shown honestly.

---

## 6. Journeys (build these end to end; in priority order)

**J1 — Photo → email (anyone).**
"Transcribe this, email me a copy, and remind me tomorrow at 9 to review it." → editable transcript → "Your email is
ready" card → Send → email in inbox → reminder set. Variant: "Email the important points to suresh@…".

**J2 — Appointment for Mummy (call, confirm, remind).**
"Book a dermatologist for Mummy tomorrow evening near Kukatpally and tell her." → search clinics/hospitals → **map
card** → "Which one?" (or the one named) → approval → call the clinic → slot captured with evidence → **Booked** only
when the clinic confirms → call Mummy in Telugu with the details → reminder call to Mummy 1 hour before + reminder to me.

**J3 — Complaint with follow-through (auto-detect + auto-find email).**
Photo of a broken product / screenshot of a problem (missed return pickup, double charge, internet down) → Chinku
identifies it ("Broken mixer · Brand X · Model Y") → searches the **official** support email/helpline (official domain
only, show the source) → drafts the complaint → approval → sends email **or** calls the helpline → records ticket
number + promised time (evidence quotes) → creates a promise → at that time asks "Did they fix it?" → if no, follow-up
call / escalation suggestion (helpline → email → grievance officer → National Consumer Helpline).

**J4 — Photo → call.**
Photo of a poster, banner, visiting card or shop board + "Call this number and ask …" → number read and validated →
approval ("Call 98xxxx21 — Sri Sai Constructions?") → call → answers with quotes.

**J5 — Connect me now (transfer).**
On a support/booking call, when a human is on the line and the user asked for it ("connect me when someone picks up"),
Chinku says "Connecting you now" and transfers via Custom API transfer to the user's phone. Verify with a test call
first; if it fails, show "Talk to them now: call <number>, ticket <id>" instead.

**J6 — Language bridge.**
"Call this Bengaluru shop and ask in Kannada if they have it; tell me in Telugu." Brief language = callee's language;
report language = user's language. (Omni agent languages currently English (India), Hindi, Telugu — add Kannada/Tamil
in the agent if available; verify before demo.)

**J7 — Scam check.** Forward/upload a suspicious SMS, screenshot or number → verdict with reasons + official sources.

**J8 — Group ask (stretch).** "Ask Ravi, Sai and Kiran if they're free Sunday lunch" → calls one after another
(1 concurrent call on our plan) → table of answers with quotes.

**J9 — Compare & choose across connected services (adapter layer, section 6A).**
"Cheapest auto home" / "Get me home before 8" / "I need soap" / "Veg lunch under ₹200 in 30 min" →
Chinku queries every connected provider through adapters → ranks by **this request's priority** → shows
"**Recommended: X — why it won**" + full comparison + next action (Book / Order / Call / Remind).
Same engine also covers **local stock and dealer quotes** (calls shops via `call` when no API exists).

---

## 6A. Connected services layer — "if they provide an API, it plugs in"

Goal: show that Chinku brings ride, food, shopping, booking, email and reminder services into **one request, one
comparison, one approval**. Build the layer for real; connect real providers where an API is available to us; use
clearly labelled **demo adapters** where it is not.

**Adapter interface (one per provider, same shape):**
```
class ProviderAdapter:
    name, category: ride|food|grocery|booking|email|reminder, status: LIVE|DEMO
    def search(request: CategoryRequest, user: UserPrefs) -> list[Offer]
    def book(offer: Offer, approval: Approval) -> Confirmation      # only after approval
    def track(reference) -> Status
Offer: provider, item/vehicle/slot, unit_price, fees, taxes, delivery_fee, final_payable,
       eta/pickup_time/slot_time, pack_size, price_per_100g (computed), match: exact|similar,
       source: api|call|web|demo, fetched_at, raw_ref
```
**Categories and providers:**
| Category | Providers (adapters) | Real path available to us tonight |
|---|---|---|
| Rides | Ola, Uber, Rapido | No public consumer booking API for us → **DEMO adapters** with realistic fares/ETAs, labelled DEMO. Real fallback: deep link to open the provider app with pickup/drop prefilled. |
| Food | Swiggy, Zomato | No public ordering API → **DEMO adapters**; real fallback: deep link + `call` the restaurant (LIVE). |
| Grocery / products | Blinkit, Zepto, BigBasket, Amazon | **DEMO adapters**; optional `search` for public prices (LIVE, with source + date). |
| Bookings (clinic, service, venue) | Any business with a phone number | **LIVE via `call`** (+ OmniDimension Google Calendar / Cal.com option if time). |
| Email | SMTP/Gmail | **LIVE** |
| Reminders | Chinku scheduler | **LIVE** |
| Shops / dealers | Any number | **LIVE via `call`** (existing dealer-quote recipe) |

**Ranking (deterministic code, explained in words):**
- Priority comes from the request + memory: lowest final price · fastest arrival / arrive-by time · earliest slot ·
  preferred brand · best value (price per 100 g / per unit) · rating if known.
- Request words override saved preferences for this request only ("I'm in a hurry today" → fastest wins).
- Compare **final payable** (fees, delivery, taxes included); unknown fees shown as "unknown", never 0.
- Exact matches and similar alternatives shown in separate groups. Constraints that fail (arrives after 8) are
  excluded with the reason.
- Output: `Recommendation {winner, why: [reasons], runner_up, what_would_change_the_winner}` + full table.

**Approve, pay and track:**
- Every booking/order needs approval showing provider, item, final amount.
- Optional **spending rule** in Settings (e.g. "auto-approve rides under ₹150") — shown as a rule, applied in code,
  logged. Default OFF.
- **Payments are DEMO only** (wallet balance + receipt shown, labelled DEMO). Never real money, never store card/UPI
  details. Real path = provider's own checkout via deep link.
- Track with order ID / booking reference / ticket in the task timeline; reminders and follow-ups attach to it.

**Preferences in memory:** Home and usual destinations, food preferences (veg, spice), budgets, preferred brands and
products, preferred providers, language, call voice.

**Honesty rule:** every DEMO offer/booking/payment shows a "Demo data" chip, and HONEST_NOTE lists every adapter's
status. The pitch says: *"Bookings are live wherever a phone call or an API is available; ride/food/grocery platforms
are shown through demo adapters — they plug in when the provider gives API access."*

---

## 7. No-instruction attachments → smart suggestions

Photo/PDF without instruction → preview + "What should I do with this?" + 2–3 actions that fit **that** image:
- Handwritten page → Transcribe · Email me a summary · Remind me to review
- Broken product / receipt problem → Raise a complaint · Find support contact
- Ticket / return screenshot → Follow up · Check pickup details
- Poster / banner / visiting card → Call and ask · Save contact
- Bill → Remind me before due date · Explain this
- Suspicious SMS → Is this a scam?
- Notice / letter → Explain in Telugu · Set a reminder for the date
If an instruction was given, skip the question and start.

---

## 8. Results, promises and memory

**Result types:** Transcript (editable, Copy, Email this) · Email (Draft → Waiting for you → Sent/Failed) ·
Call result (who, outcome, answers with quotes + status confirmed/tentative/refused/not mentioned, ticket no.,
promised time) · Booking (Requested → Booked only on confirmation) · Reminder · Research (answer + sources + date) ·
Map pick · Scam verdict · Comparison table (code-computed; unknowns shown as "unknown").

**Promises:** any commitment heard on a call ("callback by 4 PM", "technician tomorrow 10–12", "refund in 5 days")
becomes a Promise {who, what, by_when, evidence_quote, task_id} → shown on the Active task card → `follow_up` at
by_when.

**Memory:** `remember` stores user-stated facts (contacts incl. Mummy's number and language, home area, preferred
hospital, email, past tickets, preferences). Used automatically by the planner. **Never store** OTPs, passwords, card
or bank account numbers, government ID numbers. User can view and delete memory in Settings.

---

## 9. UI — Chinku App (chat-first, warm white + light blue)

Serve at `/app` (keep `?key=` protection). Mobile first (check 390×844), good on desktop.

**Tokens:** background warm white #FAF8F4 · primary light blue #DCEBFF · accent blue #2F6FED (mic, buttons, links) ·
text #14161A / #5F6672 · cards white, border #E8E4DC, radius 20px, soft shadow · font Plus Jakarta Sans
(+ Noto Sans Telugu / Devanagari) · tap targets ≥ 44px · motion 150–200 ms ease-out, respect reduced motion.
Status chips always in words: Ready · Waiting for you · Sent · Booked · Promised · Resolved · Failed · Simulated (dashed).
Show **Sent / Booked / Resolved only when confirmed.**

**First launch:** language choice (Telugu / English / Hindi) with a visible **Skip**. Voice choice (preview voices,
mapped to real OmniDimension voices if switchable per call; otherwise "preview only") shown at the first call, and in
Settings. Choices remembered.

**Priority-wise ordering:** the home screen sorts by urgency — ride leaving soon → order/email awaiting approval →
appointment today → overdue promise/callback → recent work.

**Comparison card (J9):** "Recommended · Rapido Auto · ₹92 · pickup 4 min" + "Why: cheapest final fare, arrives by
7:40 PM" + "Checked: Uber Auto ₹110 · Ola Auto ₹105" + [Book] [See all]. Demo-data chip where applicable.

**Home — exactly these, top to bottom:**
1. Big input card: **"What do you need?"** / *Type or speak in your language* — big **mic** (hold to talk),
   **Add photo** (camera/gallery/PDF), text field.
2. Example chips (each starts a real task): **Email this photo · Book a doctor · Complain & follow up · Call from a photo**.
3. **Needs you** (only when a decision waits): e.g. "Your email is ready · to suresh@… · [Send] [Edit]",
   "Call Sri Sai Clinic to book? · [Call] [Not now]".
4. **Active task** (one): e.g. "Internet complaint · Callback promised by 4 PM".
5. **Recent work:** plain rows — "Transcript ready · Copy text", "Email sent · View email",
   "Booked · Tomorrow 6 PM · Sri Sai Clinic".

**Working view (chat thread):** user bubble (photo thumbnail + editable transcript of what was said, numbers/codes
highlighted and tappable) → Chinku readback ("Transcribe · email you · remind tomorrow 9 AM") → live step list
(✓ / ⏳ / ✗ with words) → result cards. Live call card: "Calling Sri Sai Clinic… ringing → on call → reading result",
with **Connect me now** button when transfer is available. Map card: OpenStreetMap with numbered pins + list
(name, distance, open now if known, source) → tap to choose. Every Chinku answer has a 🔊 **Listen** button (OpenAI TTS).

Screenshot every screen at 390×844 and desktop; fix spacing/typography before reporting.

---

## 10. Accuracy and safety

- Structured outputs only (Pydantic); invalid → one retry → ask the user.
- Every fact has an exact source quote (image text, call transcript, or web source). No quote → not confirmed.
- Exact transcription: never summarise, never guess; `[unclear]` marks.
- Code owns arithmetic, dates, comparisons; Telugu/English/Hindi number normaliser with unit tests.
- Calls use open questions; a bare "yes" to a listed question = tentative; agent reads back key numbers before ending.
- Every call opens: "I'm an AI assistant calling on behalf of Hanish." Never share OTPs, card numbers, passwords;
  never agree to pay or buy. No emergency numbers.
- Call only numbers whose owners agreed (me, family who agreed, volunteering judges). Demo businesses (clinic, support
  line, shop) are played by consenting people and labelled SIMULATED.
- Search: official sources only for support contacts; always show source + date checked; say clearly when unsure.
- Approval before anything leaves the device; show exactly what goes to whom.
- Idempotency on webhooks, sends and calls; state persists in DynamoDB; restart resumes.

---

## 11. Demo (3 minutes) — rehearse on a phone

1. **Language bridge (30 s):** "Ask this Bengaluru shop in Kannada if they have this charger; tell me in Telugu."
   Judge's phone rings (judge plays the shop) → Telugu answer with quote. *(Fallback: Hindi if Kannada not supported.)*
2. **Complaint + follow-through + Connect me now (60 s):** photo of a broken mixer → "Raise a complaint" → official
   email found (source shown) → Send → or helpline call (judge plays support) → ticket + "callback by 4 PM" promise →
   Active task card → (demo time shortened, labelled) "Did they fix it?" → "Connect me now" transfer to my phone.
3. **Photo → email in the judge's inbox (45 s):** judge photographs a handwritten page: "Transcribe this, email me a
   copy, remind me tomorrow at 9." → transcript → Send → judge opens their own inbox.
4. **Close (15 s):** one measured number from eval + one honest limitation (no keypad menus yet).
Fallback: recorded replay of the full run, labelled as a recording.

---

## 12. Evidence for judges (`eval/results.md`, generated by script, never hand-edited)

1. **Exact transcription:** ≥10 handwritten/printed pages with typed ground truth. Baseline = one plain "read this
   image" prompt. Metrics: word error rate + hallucinated words (companion).
2. **Planning:** ≥20 real requests (voice + text; Telugu/English/mixed; single and multi-step) with expected plans.
   Metrics: correct steps, correct recipients/times; **wrong-action rate** (target 0).
3. **Call extraction:** existing results (dealer ₹714/bag → ₹35,700 in code; "moodu vandala tombhai" = 390;
   banner "65 lakhs") + new booking and support calls.
4. **Complaint contact search:** for 10 brands, % where the found email/helpline is on the official domain.
5. **Persistence:** restart mid-task → task, promise and pending approval remain.

---

## 13. Checkpoints (IST) and cut order

| By | Must be observed working |
|---|---|
| Sun 1:00 AM | Planner + conversation memory + `remember/recall` + `transcribe_image`; J1 via API |
| Sun 3:00 AM | J1 end to end (real inbox + reminder); J4 photo → call |
| Sun 5:00 AM | `search` + `map`; J2 (clinic call, Mummy call, reminders) |
| Sun 7:00 AM | J3 (auto-detect, official email search, send/call, promise, follow-up); J5 transfer verified |
| Sun 8:30 AM | J6 language bridge; J7 scam check |
| Sun 9:30 AM | J9 adapter layer + ranking engine (tests) + demo adapters for rides, food, grocery; comparison card |
| Sun 10:00 AM | UI complete (home, working view, cards, map, Listen); screenshots fixed |
| Sun 11:00 AM | Eval scripts run; restart test |
| Sun 12:00 PM | README, HONEST_NOTE, submission text, demo rehearsed + fallback recorded; pushed + deployed |
| Sun 2:00 PM | Code freeze |

**Cut order if late:** J8 → J9 grocery → J7 → J6 → J9 food → map polish (list only) → Listen button.
(Keep J9 rides — "cheapest auto home vs home before 8" shows priority ranking in 10 seconds.)
**Never cut:** J1, J3, approval gate, evidence quotes, eval, honest note.

---

## 14. HONEST_NOTE.md (keep current)

- **Built by me:** planner and tool engine, state machine, approval gates, promises/follow-up, memory, prompts and
  schemas, transcription/extraction logic, number normaliser, Chinku App UI, eval harness.
- **Reused:** OpenAI (models, vision, speech, TTS, web search if used), OmniDimension (calls, transfer), SMTP/Gmail,
  AWS (EC2, DynamoDB, scheduler), OpenStreetMap/Leaflet, Twilio WhatsApp sandbox (optional).
- **Simulated:** businesses and support agents played by consenting people; shortened follow-up timers in the demo;
  mock documents where noted; any replayed run; **ride/food/grocery provider adapters (Ola, Uber, Rapido, Swiggy,
  Zomato, Blinkit, Zepto, BigBasket, Amazon) use demo data**; **payments/wallet are demo only**.
- **Live connections:** phone-call bookings and quotes (OmniDimension), email (SMTP), reminders, web search (if verified).
- **Limits:** no keypad/IVR navigation (OmniDimension has no DTMF option); one concurrent call on our plan; cannot act
  inside other apps or log into accounts; WhatsApp sandbox unreliable for Indian numbers.

---

## 15. Submission text (update with real results before submitting)

**Project:** Chinku — tell it what you need, and it gets it done.

**What are you building?** Chinku is an everyday assistant with one question: "What do you need?" You type, speak in
Telugu, English or a mix, or show a photo. Chinku plans the steps and does the work: it reads photos and documents,
searches the web, **makes real phone calls** with an AI voice agent, emails anyone, sets reminders, and **follows up
on promises** until the task is done. It books a doctor and calls your mother to tell her; it spots a broken product in
a photo, finds the company's official support email, sends the complaint, and checks back when they promised to
respond; it waits on a support line and connects you when a human answers; it calls a shop in Kannada and tells you
the answer in Telugu. It brings ride, food, shopping and booking services into one request and recommends the option
that fits your priority — cheapest, fastest, earliest — and explains why (booking by phone is live; app providers
plug in through adapters, shown with demo data today). Every result carries proof — exact quotes, sources, ticket numbers — and nothing is sent or
called without your one-tap approval.

**What exact problem are you solving?** People know what they want, but still have to turn it into a search, a phone
call, an email and a reminder themselves — and then chase it. Indians spent over 15 billion hours on hold to customer
service in a year, about 30 hours each. Call assistants today handle calls coming *to* you; the call-for-you features
that exist abroad are not available in India or in Indian languages. Chinku does the outbound work and the
follow-through, in your language.

**Pitch:** *"Equal answers the calls that come to you. Chinku makes the calls, sends the mails, and follows up until
it's done."*

---

## 16. Final report format (at each checkpoint and at the end)

Run commands · journeys verified (with observed evidence) · test counts · eval numbers · remaining gaps ·
demo script status · next 3 actions. Observed results only.
