# CLAUDE.md — Chinku for Plumbers (Equal AI Builders Hackathon 2026)

You are my senior product, design and engineering partner. I am **Hanish**, building **solo** at the
Equal AI × OpenAI Builders Hackathon, Equal HQ Hyderabad.
**Code freeze: Sunday 22 Nov, 2:00 PM IST. Push to GitHub by 12:00 PM Sunday.**

This file replaces the previous CLAUDE.md. Read it fully before every session. It is the source of truth.
If code or APIs disagree with it, tell me, propose the fix, and update this file.

---

## 0. How you work with me

- **Reuse first.** The existing Chinku repo already has: the spec engine, state machine, Omni generic caller
  (`call_context` injection verified), OpenAI brain (perceive / route / brief / extract / decide), Twilio WhatsApp,
  SMTP email, PDF reading, DynamoDB store on AWS, dashboard, web chat at `/chat?key=...`, inbound-call agent,
  31 passing tests. **Inspect first, list what works, then extend.** Add specs and step types; do not rewrite the engine.
- **Stages:** DISCOVER / DESIGN / BUILD / REVIEW / PITCH, or normal requests.
  BUILD = implement the next working increment → run it → report what you observed.
- **Verify before depending** on any API behaviour. Record results in `DECISIONS.md`.
- **Label everything** LIVE / SIMULATED / NOT BUILT. Keep `HONEST_NOTE.md` current.
- **Never claim a result you did not observe.** If you cannot run something, mark it UNVERIFIED.
- **Scope protection:** follow section 13. Never break the demo job to add a feature.
- **Deploy after each verified increment:** `git push` + `python -m scripts.aws_deploy push`.
- **Be concise.** Short reports with the next concrete action.

---

## 1. Organizer requirements (fixed)

Solo build. Equal AI reviews every build, shortlists 10, those 10 demo live. Submission must include:
1. A working demo that runs end to end on fresh input — not slides or mock-ups.
2. Source code in a repo judges can open.
3. Evidence of results (accuracy vs baseline, outputs on new data, proof data persists).
4. A short honest note: built / reused / simulated. Only data we have permission to use.

Track: **Build Creative** — personal context into everyday AI products; show a working journey from raw input to a useful outcome.

---

## 2. Product

**Chinku is the AI office assistant for independent plumbers and small plumbing contractors.**
The plumber talks to Chinku (voice or text, Telugu / English / mixed) and gets back to work.
Chinku does the coordination around each job: calls the client, books the visit, calls shops for a missing part,
gets approval for extra charges, and turns a closing voice note into a correct company invoice.

**Problem in one line:** a plumber's day is lost to phone calls and paperwork — chasing clients, ringing shops for
parts, waiting for supervisor approvals, writing bills — and every mistake (wrong slot, wrong part, wrong amount)
costs a trip or money.

**Demo moment to protect (section 12):** a judge plays the shopkeeper and changes "exact model available today" to
"different model, available tomorrow". Chinku explains what changed, keeps the visit pending, and asks the plumber.
Then one closing voice note becomes an accurate ₹1,400 invoice.

---

## 3. Interfaces (priority order — overrides any earlier plan)

1. **Primary — Chinku App (chat-style web app, mobile first).** The plumber talks or types to Chinku here.
   Replaces the current `/chat` page (keep the key protection). Design in section 9.
2. **Primary — Phone.** The plumber can call Chinku's inbound Omni number and speak the same instructions.
   Same router, same job state.
3. **Secondary — WhatsApp (Twilio sandbox).**
   - The plumber may also message Chinku on WhatsApp (same router, same jobs).
   - Chinku **sends** messages to clients, approvers and the billing company on WhatsApp when that recipient has
     joined the sandbox. Otherwise use a **phone call** (any number) or **email** (invoices).
   - Every outbound message records its channel and delivery status; the dashboard shows it.
   - The sandbox only reaches numbers that sent the join code. Do not pretend otherwise.

Channel rule: **one router, one job store, many channels.** Channel adapters only convert input/output.

---

## 4. Stack and ownership (verify what is configured; do not assume)

| Layer | Owner | Notes |
|---|---|---|
| Phone calls (out + in), voice | OmniDimension | One generic outbound agent `chinku-caller` + inbound agent. Per-call brief via `call_context`. |
| All understanding | OpenAI | Transcribe, vision (part photos, labels), route, write briefs, extract facts, compose replies, TTS |
| Exactness | **Code** | Arithmetic, totals, availability / slot locking, approval rules, invoice numbering, dedupe |
| Messages | Twilio WhatsApp sandbox | Secondary channel, client notifications where joined |
| Email | SMTP (Gmail app password) | Invoice delivery |
| State + scheduling | AWS (EC2 us-west-2, DynamoDB via instance role, scheduler) | Stable https at the sslip.io address |
| App + API | FastAPI | Chat app, dashboard, webhooks |

---

## 5. Domain model (one persistent job ID)

```
Job
  job_id ("J24"), plumber_id, status, created_at
  issue, site_address, flat, access_notes, photos[]
  roles:
    site_contact   (person who gives access)
    approver       (who may approve extra charges)
    billing_to     (company/person receiving invoice; email/whatsapp)
    work_order     ("WO-117", optional)
  appointment:     {requested_by_client, plumber_available, confirmed_both, slot, state, history[]}
  parts_needed:    [{part_id, description, brand, model, size, qty, max_unit_price?}]
  shop_offers:     [Offer]
  dependencies:    [{type: "part_ready_before_visit", part_id, condition, state}]
  extra_work:      [ApprovalRequest]
  invoice:         Invoice
  timeline:        [Event]   # every fact links to the call/message/approval that produced it
  next_action:     {what, owner, blocked_by}

Person: name, phone(E.164), email?, whatsapp_joined: bool, language, roles across jobs
Offer:  shop, exact_model_quoted, matches_requested: exact|different_model|unknown,
        stock: usually_stocked|available_now|tomorrow|none, units_available,
        unit_price, mrp?, tax_included: yes|no|unknown, delivery_charge: number|unknown,
        condition ("₹300 each if 10+"), condition_met: bool,
        hold: none|offered|confirmed, hold_until?, pickup_address, earliest_pickup,
        source_call_id, evidence_quote, checked_at, superseded_by?
ApprovalRequest: id, scope, amount, reason, sent_to, channel, version,
        response: approved|rejected|question|pending, responder, responder_is_designated: bool,
        approved_scope, approved_amount, at, evidence
Invoice: number, date, lines[{item, qty, unit_rate, amount, source}], labour, tax_config, total,
        state: draft|awaiting_plumber|approved|sent|delivery_failed|paid, send_attempts[], idempotency_key
Event:  ts, kind, actor, channel, summary, payload_ref, simulated: bool
```

Keep distinct, never merged: **supplier cost**, **approved customer charge**, **completed work**.
Keep distinct: **client requested**, **plumber available**, **both confirmed**.
Keep distinct: **usually stocked**, **available now**, **hold offered**, **reservation confirmed**.

---

## 6. Workflow (build one complete job; see section 13 for order)

### 6.1 Create job + contacts
Input example: "Chinku, call this customer 98xxxxxx21. Kitchen tap repair at Flat 304. I can visit tomorrow
11–1 or after 4. Send the final invoice to the maintenance company under WO-117."
- Extract everything known; ask **only** for what the next step needs.
- Multiple numbers in one message → ask which to call. Multiple open jobs → ask which job before changing anything.
- One person may hold several roles.

### 6.2 Call client and book
- Brief: Chinku calls on behalf of the named plumber; confirms issue, full address, flat/floor, access, who will be
  present; asks for a photo only if useful; offers the plumber's windows.
- **Re-check slot availability in code at save time** (conditional write / lock). If taken, offer the next window.
- Confirm to plumber and client (client: WhatsApp if joined, else short confirmation call).
- No answer → record attempt, retry per policy (1 retry after 5 min), tell plumber.
- Late / cancel (stretch): call affected client with revised time; original stays visible until accepted.

### 6.3 Source a missing part
Input: photo / label / voice note "Job 24, two of these valves, check my regular shops, exact model, price, pickup".
- Vision reads brand, model, size, qty. Unreadable or ambiguous → ask the plumber. Never guess a model.
- Shops come only from plumber-supplied contacts.
- Each shop call asks: exact model and size in stock **today**? units? actual selling price per unit? MRP?
  tax and delivery included? pickup address and earliest time? can you hold the quantity, until when?
- Save each offer separately with call id, evidence quote, time. Compare totals **in code**.
  Unknown charges shown as **unknown**, never zero. Conditional price applies only if the condition is met.
- Substitute model → send actual model and price to the plumber; **the plumber decides compatibility.**
- Reserve/purchase only within authority the plumber gave. Default: no commitments.

### 6.4 Part readiness ↔ visit
Rule example: "If Srinivas has the exact valve under ₹500, ask the customer whether I can come between 4 and 6."
- Store as a dependency with a machine-checkable condition (shop, exact model, max price).
- Condition met → proceed to the client call. Not met (different model / tomorrow / over limit) → report what
  differs, keep the visit **pending**, ask the plumber.
- A promised callback ("I'll call you back at 5") is its own event: who calls whom, when, what is unresolved.

### 6.5 Extra-work approval
Input: "Job 24, hose also needs replacement, extra ₹300 including fitting, ask the supervisor."
- Send to the **designated approver** only: job ref, work, amount, reason. Accept approve / reject / question.
- Record responder, time, exact scope and amount. Tell the plumber.
- Price/scope change → new version of the request. A resident's "yes" counts only if the resident is the approver.

### 6.6 Closing voice note → invoice
Input: "Job 24 finished. Fitted two angle valves at ₹350 each and one hose at ₹300. Labour ₹400.
The third valve was unused and returned. Send the bill to the company."
- Extract fitted items, qty, rates, labour, adjustments. Returned/unused parts are **excluded**.
- Reconcile with the job: approvals, quoted parts. Flag discrepancies (e.g. hose ₹300 approved? yes/no).
- If a spoken total conflicts with line items, read back the difference and ask.
- Tax only per configured business settings (default: none).
- Readback: "Job 24: two valves ₹700, hose ₹300, labour ₹400. Total ₹1,400. Send to the maintenance company
  against WO-117?" — expected total **₹1,400**.
- Voice corrections supported. After "yes": generate PDF (business details, invoice no., date, recipient, site,
  job ID, WO, lines, qty, rates, labour, tax, total) → send to billing contact via configured channel.
- Idempotent send (one invoice number, retries never duplicate). States: draft → awaiting_plumber → approved → sent
  / delivery_failed → paid (only when the plumber confirms payment).

---

## 7. Accuracy (top priority — how we get it)

1. **Structured outputs only.** Every OpenAI call returns JSON validated by Pydantic. Invalid → one retry → ask the user.
2. **Evidence per fact.** Every extracted field carries `value`, `status`
   (confirmed / tentative / refused / not_mentioned / conflicting) and an exact `evidence_quote`.
   No quote → not confirmed. Never fill a value without a quote.
3. **Open questions, not leading ones.** Briefs ask "What is the price per unit?" not "Is it ₹350?".
   A bare "yes" to a listed question = tentative.
4. **Numbers.** Normalise spoken Telugu/English/Hindi numbers ("moodu vandala yabhai" = 350, "padhihenu" = 15,
   "lakh", "per piece" vs "total") with a dedicated normaliser + unit tests. Keep the unit the speaker used.
   The voice agent repeats numbers exactly as heard and reads back key numbers before ending the call.
5. **Code owns arithmetic and rules.** Totals, comparisons, conditions, slot conflicts, approval validity, invoice
   totals — all deterministic Python with tests. The LLM never adds numbers.
6. **Two-pass for money.** Pass 1: LLM extracts line items. Pass 2: code reconciles against job state
   (approvals, offers, returned parts) and lists discrepancies. Anything unreconciled → ask the plumber.
7. **Readback before any consequential action** (book slot, send approval request, send invoice, commit to a shop).
8. **Model choice.** Best available model for extraction and invoices (`OPENAI_MODEL_BRAIN`); a cheaper model only
   for routing small talk. Already observed: the cheaper model misread a Telugu number (885 vs 390) — keep it out of
   extraction.
9. **Ask, don't guess.** Ambiguous job, number, model or size → one short clarifying question.
10. **Eval set** (section 11) decides prompt changes. Never tune on the fresh hold-out set.

---

## 8. Brain prompts (store in `app/brain/prompts/`, versioned)

Reuse existing: Base Agent Prompt (Omni), Brief Writer, Extractor, Router, Reply composer. Changes:
- **Brief Writer:** add caller persona "calling on behalf of plumber {plumber_name}"; role-specific question sets
  for `client_booking`, `shop_part_check`, `approval_request`, `client_reschedule`; always end with a numeric readback.
- **Extractor:** add schemas for `Offer`, `BookingResult`, `ApprovalResponse`, `InvoiceDraft`; add `conflicting` status.
- **Router:** output `{intent, job_ref | "new" | "ambiguous", entities, missing, clarifying_question}`.
  Intents: create_job, book_visit, source_part, set_dependency, request_approval, approval_response,
  close_job_invoice, correct_detail, report_delay, cancel, status_query, small_talk.
- **Reply composer:** plumber-facing, short, Telugu-English by default, lead with the answer, mark tentative items,
  end with one clear action ("Send cheyyala?").

---

## 9. Chinku App — chat-style UI (primary interface)

Goal: feels like WhatsApp/ChatGPT, built for a plumber on a phone with dirty hands.

**Mobile layout (default):**
- Top bar: "Chinku" + active job chip ("J24 · Flat 304 · Kitchen tap") → tap to switch jobs.
- Chat stream: plumber bubbles right, Chinku bubbles left. Chinku bubbles can contain **cards**:
  - Job card (next action, owner, blocker).
  - Live call card: "Calling Srinivas Hardware… ringing → on call → reading result" with a simulated/live label.
  - Shop comparison card: one row per shop — exact model ✓/✗, stock state chip, unit price, total, hold, address;
    unknown values shown as "unknown"; tap a row → evidence quote.
  - Approval card: scope, amount, approver, status chip.
  - Invoice card: lines, total, [Send] [Correct] buttons, PDF preview link.
- Status chips: **Tentative** (amber), **Confirmed** (green), **Pending/Blocked** (grey), **Failed** (red),
  **Simulated** (dashed outline). Meaning never by colour alone — always text.
- Bottom: big **hold-to-talk mic** (primary), text box, 📎 photo/PDF. While recording: waveform + "Release to send".
  After send: show transcript in the bubble so the plumber can see what Chinku heard; tap to correct.
- Chinku replies also play as short TTS audio (toggle).
- Quick actions under the last card: "Yes, send", "Change", "Call again", "Show job".

**Desktop / dispatcher + demo view (`/dashboard`):** three columns — jobs list · chat · job detail
(appointment, roles, shop offers table, approvals, invoice, **timeline** with links to the call/message/approval
that produced each fact). When a shop corrects its price, only that cell and the total update (highlight briefly).

Tech: keep it simple — server-rendered HTML + small vanilla JS or a single-file React via CDN; SSE or polling for
live updates; MediaRecorder for audio. Must work on a phone browser over https. Keep the `?key=` protection.

---

## 10. Reliability rules (keep existing, add these)

- Idempotency on Twilio `MessageSid`, Omni `call_id`, invoice `idempotency_key`. Duplicate or out-of-order webhooks
  must not double-book, double-approve or double-send.
- Slot booking uses a conditional write so two clients cannot take one slot.
- State persists in DynamoDB; restart mid-job → resumes.
- Every outbound action logs channel + status; failures retry with backoff and are reported to the plumber.
- Confirmation gate before: calling a new number, sending approval request, booking, sending invoice, any commitment.
- Any error → Chinku tells the plumber in plain words; never silence.

---

## 11. Evidence for judges

1. **Accuracy vs baseline** on labelled real call transcripts (shop calls, client bookings, approvals) and
   closing voice notes:
   - Baseline: one plain prompt "summarise this call / make an invoice".
   - Chinku: schema + statuses + evidence + code reconciliation.
   - Main metric: field accuracy. Companion metric: **wrong-confirmed rate** (tentative/missing reported as
     confirmed) and **invoice total errors**. Report counts. Keep a fresh hold-out set (≥5) never used for tuning.
2. **Focused checks (automated tests + at least one live where possible):**
   - Client requests a slot another booking just took.
   - Same brand, different model.
   - Price applies only to a larger quantity.
   - Stock tomorrow / reservation expired.
   - Resident says yes but only the supervisor can approve.
   - Unused part returned; invoice corrected by voice.
   - Duplicate webhook; invoice send retried (no duplicate).
3. **Persistence:** restart the server mid-job; the job resumes on the dashboard.
4. `eval/results.md` is generated by script, never hand-edited.

---

## 12. Demo (90 seconds) — protect this

1. Plumber (me) holds the mic: "Chinku, Job 24 ki rendu angle valves kavali, Srinivas and Ravi shops ki call chesi
   exact model, price, pickup kanukko. Srinivas daggara 500 lopu unte customer ni 4 to 6 adugu."
2. Judge's phone rings — judge plays Srinivas: "Different model undi, exact one repu vastundi."
3. Chinku App shows: offer card "Different model · Tomorrow", dependency **not met**, visit **pending**, and asks:
   "Srinivas has model X, not Y, and exact model only tomorrow. Wait, take X, or check Ravi?"
4. Plumber: "Ravi ni check chey." → second shop call (or cached simulated, clearly labelled).
5. Closing voice note → readback "Total ₹1,400 … send?" → "Yes" → PDF invoice delivered; dashboard timeline shows
   every fact linked to its call/approval.
6. Close with one measured accuracy number and one honest limitation.

Fallback: a recorded replay of the same run, labelled as a recording.

---

## 13. Build order and checkpoints (IST)

| By | Must be working (observed) |
|---|---|
| Sat 9:00 PM | Job model + roles + router intents; create job from voice in Chinku App; job card shows |
| Sat 11:30 PM | Client booking call with slot re-check and confirmations |
| Sun 2:00 AM | Part sourcing: 2 shop calls, offers, comparison, different-model/tomorrow handling, dependency rule |
| Sun 4:00 AM | Extra-work approval with designated-approver rule |
| Sun 6:00 AM | Closing note → reconciliation → readback → PDF → idempotent send (₹1,400 case passes) |
| Sun 8:00 AM | Chat UI polish (cards, chips, mic, transcript), dashboard timeline |
| Sun 10:00 AM | Eval vs baseline generated; focused checks pass; restart test |
| Sun 12:00 PM | README, HONEST_NOTE, demo rehearsed + fallback recording, pushed + deployed |
| Sun 2:00 PM | Code freeze |

Cut order if late: delay/reschedule → WhatsApp client notifications (use calls) → second shop live (simulate,
labelled) → UI polish. **Never cut:** the demo moment, invoice accuracy, evidence, honest note.

Old features (dealer quotes, banner, passbook, appointment, warranty) stay in the repo and keep passing tests, but
are not in the demo or README headline.

---

## 14. Safety, consent, honesty

- Call only numbers whose owners agreed (me, friends, judges who volunteer). Mock shops and clients for testing.
- Every call says it is an AI assistant calling for the named plumber.
- No payments, no purchases, no reservations beyond the plumber's stated authority.
- Mask phone numbers in the UI except last 4 digits. Keep the chat key out of the repo and submission.
- HONEST_NOTE: built by me (engine, specs, brain prompts, schemas, reconciliation, UI, eval); reused (OmniDimension,
  Twilio sandbox, OpenAI, AWS, SMTP); simulated (test shops/clients played by consenting people, mock part photos,
  any replayed calls); limits (sandbox WhatsApp only reaches joined numbers; Omni plan = 1 concurrent call, so shops
  are called one after another).

---

## 15. Final report format (when I ask, and at the end)

Run commands · features actually verified (with observed evidence) · test results (counts) · remaining integration
gaps · 90-second demo script · next 3 actions. Use observed results only.  