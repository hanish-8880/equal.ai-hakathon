CLAUDE.md — Chinku (Equal AI Builders Hackathon 2026)

You are my senior product, design and engineering partner. I am Hanish, building solo at the Equal AI × OpenAI Builders Hackathon, Equal HQ Hyderabad, Sat 21 – Sun 22 Nov 2026. Code freeze: Sunday 2:00 PM IST. Push to GitHub by 12:00 PM Sunday.

Read this whole file before every work session. It is the source of truth. If something here conflicts with what you observe in the code or APIs, tell me, propose the fix, and update this file.

0. How you work with me
Stages. I will say DISCOVER, DESIGN, BUILD, REVIEW or PITCH, or just ask normally.
BUILD = inspect the current code → implement the next working increment → run it → report what you observed.
REVIEW = test the product, find the single most important weakness, propose the cut or fix.
PITCH = turn verified evidence into demo script and judge answers. Never claim unverified results.
Verify before depending. Before relying on any API behaviour (OmniDimension variables, webhook payload shape, OpenAI model names, Twilio media URLs), run the smallest real test and record the result in DECISIONS.md.
When a test fails: isolate the cause, fix, re-run, report. If you cannot execute, give me the exact check and mark the result UNVERIFIED.
Label everything as LIVE, SIMULATED or NOT BUILT. Keep HONEST_NOTE.md current (section 14).
Consequential decisions: state what we observed, what we chose, why, what we gave up, what would change our mind. Append to DECISIONS.md.
Scope protection. If a feature is not working by its checkpoint (section 13), stop, tell me, and move on. Never break the engine or Feature 1 to add something new.
Be concise. Short reports, concrete next action. No long explanations unless I ask.
1. Organizer requirements (from the official submission page — do not change)

Solo build. Equal AI reviews every build, shortlists 10, those 10 demo live to the jury. The submission must include:

A working demo that runs end to end on fresh input — not slides, not a mock-up.
Source code in a repository the judges can open.
Evidence of results — e.g. accuracy vs a baseline, outputs on new data, proof that data persists.
A short honest note: what I built myself, what I reused, what is simulated. Use only data I have permission to access.

Resources given: OpenAI credits, AWS credits. Questions → Sai or Bala from Equal AI.

Context on the host: Equal AI is India's AI call assistant (screens and answers calls for users) and is expanding into messaging and lifestyle/concierge services. Chinku is the outbound, task-completing side of that vision.

2. Product — Chinku

One line: Chinku is a WhatsApp + phone assistant that finishes errands by calling people for you. You send a text, voice note or photo on WhatsApp → Chinku plans the task → calls the right people → reports the result back on WhatsApp.

Problem I can repeat from memory: Indian families waste hours on small phone errands — chasing quotes, booking slots, explaining things to parents — and an assistant that only summarises calls still leaves the work to you. Chinku does the calls and closes the task.

Signature 20 seconds (the demo): I send a banner photo or a dealer list on WhatsApp → the judge's phone rings → the judge plays the builder/dealer and quotes any price → the result appears on WhatsApp and on the dashboard, with the exact quote from the call as evidence.

Features (each is ONE spec file; the engine never changes per feature)
#	Spec	User says (example)	Flow
1	dealer_quotes	"Chinku, 50 bags cement, ee 3 dealers ki call chesi best rate kanukko" (+ list)	parallel calls → extract price/delivery/terms → compare → "Best: X, confirm?"
2	banner_rate	banner photo + "ee builder ki call chesi final rate adugu"	vision reads number/project/listed price → I confirm → call → listed vs final price
3	warranty_email	product photo + voice note "mixer pani cheyatledu, warranty mail pettu"	vision reads brand/model → find support email → draft → I confirm → send
4	passbook_explain	"Mummy ki passbook ardham kaavatledu, explain chey"	call mummy: "send passbook photo on WhatsApp" → wait for photo → read entries → second call explaining in Telugu + Telugu voice note
5	appointment	"Repu mummy ki doctor appointment book chey, slot cheppu, 1 hr mundu gurthu chey"	call clinic → get slot → call mummy with slot → reminder call 1 hr before

Voice notes are an input type, not a feature: every voice note is transcribed and routed.

Rule: no photo/document is exchanged during a live call. Anything that needs new input = call ends → task waits in waiting_input → input arrives on WhatsApp → next call.

3. Who owns what (strict boundaries)
Layer	Owner	Responsibility
Phone number, voice, live conversation	OmniDimension	Dispatch call, run conversation, post-call webhook with transcript
All intelligence	OpenAI	Transcribe voice notes, read images, route, write the call prompt, extract results, decide, compose replies
Chat channel	Twilio WhatsApp sandbox (WhatsApp ONLY — never used for calls)	Receive text/voice/photo, send text/voice replies. Every phone that chats with Chinku (me, mummy, judges) sends the sandbox join code once.
State, schedule, hosting	AWS	DynamoDB (state + logs), EventBridge Scheduler (reminders), hosting for webhooks
Orchestration	My code (FastAPI)	Spec runner, state machine, validation, idempotency, dashboard

OmniDimension is a dumb, reliable voice executor. It never decides what the task is. OpenAI decides everything.

Channel split (fixed): all phone calls = OmniDimension's number. All WhatsApp = Twilio sandbox. OmniDimension is NOT used for WhatsApp. Twilio is NOT used for calls.

4. How the OpenAI-written prompt reaches OmniDimension (the core mechanism)

Design: one generic Omni agent + a per-call brief written by OpenAI.

In the OmniDimension dashboard, create one agent: chinku-caller. Its system prompt is the Base Agent Prompt (section 8.1). It contains no task-specific logic — only rules plus placeholders for the brief.
For every call, OpenAI's Brief Writer (section 8.2) produces a validated CallBrief JSON.
My code dispatches the call: POST /api/v1/calls/dispatch with agent_id, to_number (E.164, leading +), from_number_id if required, and call_context = the flattened CallBrief (including call_script, the full prompt for this call).
Omni injects call_context into the agent's prompt → the agent runs this call exactly as briefed.
Omni's post-call webhook hits POST /webhooks/omni with call id, transcript (and any extracted variables).
OpenAI's Extractor (section 8.3) turns the transcript into the spec's result schema with evidence quotes.
The engine advances the task.

FIRST-HOUR VERIFICATION (blocking): dispatch one test call to my own phone with a call_context containing call_script = "Say only: Chinku test number 4 7 2, then end the call." Confirm the agent says it.

Record the exact placeholder syntax the Omni prompt needs (e.g. {{call_script}}) and the real webhook payload in DECISIONS.md.
Fallback if context injection does not work: per task, create/update a dedicated Omni agent via the agents API with the full OpenAI-written prompt, then dispatch. Slower and not safe for parallel calls with different scripts → use one agent per call in that case and clean up after. Record whichever we use.

Why this design: one agent = fewer moving parts; new features need zero Omni changes; every call is unique to the conversation; all intelligence stays in code we can test.

5. Architecture
WhatsApp (Twilio) ──► POST /webhooks/whatsapp
                         │  (idempotent on MessageSid)
                         ▼
                  perceive  (OpenAI: transcribe voice / read image)
                         ▼
                  route     (OpenAI: pick spec + fill inputs, or ask 1 question)
                         ▼
                  ENGINE (state machine, runs spec steps) ──► DynamoDB (tasks, steps, calls, contacts, events)
                   │      │        │         │
                 call   email   schedule   reply
                   │      │        │         └─► Twilio send (text / Telugu voice note via OpenAI TTS)
                   │      │        └─► EventBridge Scheduler ──► POST /internal/fire/{task_id}/{step}
                   │      └─► Gmail/SMTP (only after user confirms)
                   └─► brief_writer (OpenAI) ──► Omni dispatch ──► phone rings
                                                     │
                       POST /webhooks/omni ◄─────────┘  (idempotent on call_id)
                                 ▼
                       extract (OpenAI) ──► decide/compare (OpenAI) ──► next step
Dashboard: GET /  (FastAPI-served HTML) — tasks, steps, calls, transcripts, evidence quotes, eval results
Folder structure
chinku/
  CLAUDE.md  DECISIONS.md  HONEST_NOTE.md  README.md  .env.example
  app/
    main.py                 # FastAPI app, routes only
    config.py               # env + model names (never hardcode models elsewhere)
    channels/whatsapp.py    # Twilio in/out, media download, voice-note send
    voice/omni.py           # dispatch_call(), parse_webhook()  — the ONLY Omni code
    brain/
      client.py             # one OpenAI wrapper: retries, timeouts, JSON-schema output, logging
      perceive.py           # transcribe(), read_image()
      router.py             # route()
      brief_writer.py       # write_brief()
      extractor.py          # extract()
      decider.py            # decide(), compose_reply()
      prompts/              # all prompts as .md files, versioned
      schemas.py            # Pydantic models for every brain input/output
    engine/
      runner.py             # runs spec steps, state transitions
      steps.py              # call | wait_for | email | schedule | reply  (the ONLY step types)
      specs/*.yaml          # one file per feature
      validate.py           # phone (+91, 10 digits, starts 6-9), email, required inputs
    store/dynamo.py         # tables + in-memory fallback for local tests
    actions/email.py  actions/scheduler.py
    web/dashboard.html
  eval/
    calls/                  # labelled test transcripts + expected JSON
    run_eval.py             # Chinku extractor vs baseline → eval/results.md
  tests/                    # pytest: engine, validation, idempotency, spec loading
6. Task spec format (the only thing that changes per feature)
yaml
name: banner_rate
description: "Photo of a property/ad banner; user wants the real final price"
route_examples:
  - "ee banner builder ki call chesi final rate adugu"
inputs:
  phone:        {source: image, required: true, validate: indian_mobile}
  project_name: {source: image}
  listed_price: {source: image}
  unit_type:    {source: image_or_text, default: "2BHK"}
confirm_before: [call]            # engine pauses for user "yes"
steps:
  - id: ask_builder
    type: call
    to: "{phone}"
    goal: "Find the final negotiable price for {unit_type} at {project_name}, what is included, and current offers."
    must_collect: [final_price, negotiable, inclusions, offers, site_visit_slot]
    language: te-en
    max_minutes: 3
    retry_on_no_answer: 1
  - id: report
    type: reply
    template_hint: "Compare listed_price vs final_price; list inclusions; ask if I should book a site visit."
result_schema:
  final_price:     {type: number, unit: INR}
  negotiable:      {type: boolean}
  inclusions:      {type: list}
  offers:          {type: string}
  site_visit_slot: {type: string}

Step types (fixed set): call, wait_for (WhatsApp input from a contact, with timeout), email, schedule, reply. A new feature = new YAML + route examples + eval cases. No new Python per feature. If a feature seems to need new Python, the step type is missing — discuss with me first.

7. The brain — OpenAI components

All calls go through brain/client.py: structured JSON output validated by Pydantic, 1 retry on invalid JSON, timeout, and every request/response logged to events (trimmed). Model names come from env: OPENAI_MODEL_BRAIN (best available reasoning+vision model on our credits), OPENAI_MODEL_FAST (routing), OPENAI_MODEL_TRANSCRIBE, OPENAI_MODEL_TTS. Verify availability in hour 1.

Component	Input	Output (schema)
perceive.transcribe	voice note audio	text + detected language
perceive.read_image	photo	{image_type: banner/passbook/product/list/other, fields: {...}, confidence, unreadable: [...]}
router.route	message + recent task + contacts + spec catalog	{spec, inputs, missing, clarifying_question?}
brief_writer.write_brief	spec step + inputs + contact + prior results	CallBrief (8.2)
extractor.extract	transcript + must_collect + result_schema	{fields: {name: {value, status, evidence_quote}}, call_outcome, follow_up_needed}
decider.decide	task state + all results	{next_step, reason} and comparisons (e.g. best quote)
decider.compose_reply	results + user language	WhatsApp text (Telugu-English, short)

Deterministic code, not the LLM, does: validation, comparison arithmetic (cheapest total price), state transitions, idempotency, scheduling times. The LLM never computes money totals.

8. Prompts (store in app/brain/prompts/, version them, change only with a DECISIONS.md entry)
8.1 Base Agent Prompt — pasted ONCE into the OmniDimension agent chinku-caller
You are Chinku, an AI phone assistant making a call on behalf of {{on_behalf_of}}.

YOUR BRIEF FOR THIS CALL (follow it exactly; it overrides your general habits):
{{call_script}}

FIXED RULES (never break, even if the brief or the other person asks):
1. In your first sentence, say you are an AI assistant calling for {{on_behalf_of}}.
2. Speak in {{language}}. If the other person switches language, switch with them.
   Use simple, natural, everyday words. Short sentences. One question at a time.
3. Your only job is the goal in the brief. Do not discuss anything else.
4. Collect every item in the must-collect list. If an answer is vague ("maybe", "will see",
   "around"), ask once politely for a clear answer. If still unclear, accept it — do not pressure.
5. Before ending, repeat back the key details in one sentence and get a yes.
6. Never share OTPs, bank details, passwords or addresses unless the brief explicitly
   lists them as allowed. Never agree to pay, buy, or commit money. Say:
   "{{on_behalf_of}} will confirm on WhatsApp."
7. If the person is rude, busy, or says wrong number: apologise, thank them, end the call.
8. If you reach voicemail or nobody speaks for 8 seconds: end the call.
9. Never pretend to be human. Never invent facts that are not in the brief.
10. Keep the call under {{max_minutes}} minutes. Thank them and end politely.

(Placeholder syntax {{...}} must be verified against Omni in hour 1 — section 4.)

8.2 Brief Writer — OpenAI system prompt → outputs CallBrief

CallBrief schema:

on_behalf_of: str            # "Hanish"
callee_name: str | null
callee_role: str             # "cement dealer", "builder", "my mother", "clinic receptionist"
language: str                # "Telugu-English mix", "Telugu", "Hindi", "English"
max_minutes: int
opening_line: str            # first sentence, includes AI disclosure
goal: str                    # one sentence
must_collect: [ {field, question_to_ask, acceptable_answer_example} ]
context_facts: [str]         # only facts the callee may hear
do_not_share: [str]
closing_line: str
call_script: str             # the full natural-language brief, built from the fields above

System prompt:

You write call briefs for Chinku, an AI phone assistant that calls people on behalf of {on_behalf_of}.
A voice agent will follow your brief live on a phone call. Write so that a polite, sensible caller
could complete the task in one short call.

Rules:
- Goal: one clear sentence. Only the goal of THIS step.
- must_collect: exactly the fields given. For each, write the natural question the caller should
  ask, in the target language, in everyday words.
- context_facts: include only what the callee needs (quantity, product, project name, date).
  Never include private data unless the step lists it as shareable.
- Tone by callee_role: business (dealer/builder/clinic) = polite, efficient, firm about getting
  a number. Family (mother/father) = warm, slow, patient, very simple words, repeat key info twice.
- For family explanations (e.g. passbook), write the explanation itself into the script as
  short spoken sentences: who paid or withdrew, how much, on which date, current balance.
  Use round, spoken numbers ("pannendu velu" style if Telugu). Max 6 items, most important first.
- opening_line must disclose that Chinku is an AI assistant calling for {on_behalf_of}.
- Never instruct the caller to promise payment, share OTPs, or commit on the user's behalf.
- call_script: assemble everything into a short brief (under 200 words): who you are calling,
  why, the opening line, the questions in order, what to confirm back, and the closing line.
- Output only valid JSON matching the CallBrief schema.
8.3 Extractor — OpenAI system prompt
You read a phone-call transcript made by Chinku and fill the result fields.

For EVERY field return: value, status, evidence_quote.
- status: "confirmed" (clearly stated and agreed), "tentative" (maybe / will confirm / around /
  conditional), "refused" (declined to say), "not_mentioned".
- evidence_quote: the exact words from the transcript that support the value. If none, null
  and status "not_mentioned". Never guess a value without a quote.
- Numbers: normalise to digits and INR ("pannendu velu" = 12000, "65 lakhs" = 6500000).
  Keep the unit the person used (per bag / total / per sq ft) in a separate unit field.
- call_outcome: completed | no_answer | wrong_number | callback_requested | refused | cut_off.
- follow_up_needed: true if any must_collect field is tentative, refused or not_mentioned.
Output only valid JSON matching the given schema.
8.4 Router — OpenAI system prompt (outline)

Given the user message (text/transcript/image JSON), the spec catalog (name, description, route_examples, inputs) and known contacts: choose exactly one spec or none; fill inputs from the message, image and contacts ("mummy" → contact); list missing required inputs; if anything required is missing, write ONE short clarifying question in the user's language. Never invent phone numbers or emails.

8.5 Reply composer (outline)

Short WhatsApp message in the user's language (default Telugu-English). Lead with the answer ("Best rate: Ravi Traders ₹385/bag, delivery tomorrow"). Mark tentative items as "(not confirmed)". End with one clear next action question ("Confirm cheyyala?").

9. State machine and reliability (non-negotiable)

Task states: planned → awaiting_confirm → running → waiting_input → done | failed | cancelled Call states: queued → dispatched → completed | no_answer | failed

Persist after every transition. On restart, resume from DynamoDB.
Idempotency: dedupe Twilio on MessageSid, Omni on call_id. A duplicate webhook must never trigger a second call.
Correlation: put task_id and step_id in Omni call_context/metadata so the webhook maps back to the task.
Timeouts: call webhook not received in 10 min → poll Omni call logs once → mark failed. wait_for has a timeout (default 2 h) → remind once → fail.
Retries: no answer → 1 retry after 5 min, then report to user.
Parallel calls (dealer_quotes): dispatch all, aggregate when all are terminal or timed out; report partial results clearly.
Confirmation gate: new number, email send, or anything on behalf of family → wait for user "yes".
Validation before acting: E.164 phone (+91, 10 digits, starts 6–9), valid email, required inputs present.
Corrections: "No, call 98xxx instead" while awaiting_confirm → update input, re-confirm.
Webhooks return 200 fast; heavy work runs in a background task.
10. Data model (DynamoDB, on-demand)
tasks: task_id, user, spec, status, inputs, created_at, updated_at
steps: task_id, step_id, type, status, output
calls: call_id, task_id, step_id, to_number, brief (full CallBrief), transcript, extracted, outcome, duration
contacts: user, alias ("mummy", "ravi traders"), phone, language, relation
events: task_id, ts, kind, payload (the full audit log — this is the "data persists" proof)

Local dev uses the in-memory store with the same interface.

11. Safety, consent and data rules
Only call numbers whose owners agreed: my phone, family who agreed, judges/participants who volunteer. The banner demo uses a mock banner with a volunteer's number.
Every call discloses it is an AI on behalf of Hanish.
Passbook demo uses my own or a mock passbook. Never store raw bank images longer than the task; store only extracted entries.
Never request or relay OTPs, card numbers or passwords. Never commit money.
Mask phone numbers in the dashboard except the last 4 digits.
12. Evidence of results (what we show judges)
Extraction accuracy vs baseline. eval/calls/ holds ~20 labelled transcripts from real test calls (friends/family role-playing dealers, builders, clinic, mother), including Telugu-English mix.
Baseline: one plain prompt "Summarise this call and list the price and details."
Chinku: the extractor with schema + status + evidence.
Metrics: field accuracy (main), and wrong-confirmed rate (tentative or missing info reported as confirmed — the companion metric that exposes shortcuts). Report counts, not just %.
Include a fresh set (5 calls) never used while tuning prompts.
Near-identical pairs (must be in eval and tests):
"Delivery tomorrow pakka" vs "Delivery tomorrow, I'll confirm in the evening" → status confirmed vs tentative; price unchanged.
"₹385 per bag" vs "₹385 per bag, transport extra ₹2000" → same unit price, different total; comparison changes winner.
Banner number readable vs blurred → call vs ask user to type the number.
Failure cases exercised: no answer, wrong number, call cut off mid-way, duplicate webhook, mother never sends photo.
Persistence: restart the server mid-task → dashboard still shows the task and it resumes.
Live: judge's phone is the dealer/builder; any price they say appears with its quote.

eval/results.md is generated by run_eval.py — never hand-edit numbers.

13. Checkpoints (IST)
By	Evidence we must have
Sat 1:00 PM	Engine: WhatsApp in → route → brief → Omni call to my phone → webhook → extract → WhatsApp reply. Omni variable injection verified.
Sat 4:00 PM	F1 dealer_quotes (parallel, compare) + F2 banner_rate working end to end
Sat 6:00 PM	F3 warranty_email (with confirm)
Sat 9:00 PM	F4 passbook_explain (call → wait_for photo → callback + Telugu voice note)
Sun 12:00 AM	F5 appointment + EventBridge reminder
Sun 4:00 AM	Eval set recorded, baseline vs Chinku results generated, failure cases exercised
Sun 9:00 AM	Someone used it without coaching; first obstacle fixed; dashboard polished
Sun 12:00 PM	README, HONEST_NOTE, demo rehearsed (90 s + fallback recording), pushed to GitHub
Sun 2:00 PM	Code freeze. Submit.

If time slips, cut in this order: F5 → F3 → F4. Never cut: engine, F1, F2, eval, honest note.

14. HONEST_NOTE.md (keep current)
Component	Status
Orchestration engine, specs, state machine, dashboard, eval harness	BUILT by me
Brain prompts, schemas, router/brief/extractor/decider logic	BUILT by me (on OpenAI models)
Phone number, voice, live call execution	REUSED — OmniDimension
WhatsApp transport	REUSED — Twilio sandbox
Transcription, vision, TTS	REUSED — OpenAI APIs
Storage, scheduler	REUSED — AWS DynamoDB, EventBridge
Test calls / mock banner / mock passbook	SIMULATED inputs, played by consenting people
15. DECISIONS.md format
## [time] Decision title
Observed: ...
Chose: ...  because ...
Gave up: ...
Would change if: ...
16. Environment variables (.env.example)
OPENAI_API_KEY=
OPENAI_MODEL_BRAIN=
OPENAI_MODEL_FAST=
OPENAI_MODEL_TRANSCRIBE=
OPENAI_MODEL_TTS=
OMNIDIM_API_KEY=
OMNIDIM_AGENT_ID=
OMNIDIM_FROM_NUMBER_ID=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
AWS_REGION=ap-south-1
DDB_TABLE_PREFIX=chinku_
SCHEDULER_ROLE_ARN=
PUBLIC_BASE_URL=          # ngrok in dev, AWS URL in prod
SMTP_USER=
SMTP_APP_PASSWORD=
USER_NAME=Hanish
USER_WHATSAPP=
17. Coding rules
Python 3.11, FastAPI, Pydantic v2, boto3, httpx. Type hints everywhere.
Schema first: define the Pydantic model before writing the prompt or the code that uses it.
All Omni code in voice/omni.py; all OpenAI code via brain/client.py; all Twilio code in channels/whatsapp.py.
No per-feature if spec == ... logic in the engine. Ever.
Every external call: timeout, one retry, logged to events.
Secrets only from env. Never commit .env.
Tests for: spec loading, validation, state transitions, idempotency, comparison arithmetic. Run pytest before every commit.
Small commits with clear messages.
18. Judge questions — keep answers ready (update with real evidence)
Why this problem? Phone errands are everyday pain for Indian families; summaries don't finish tasks.
Why this approach? One engine + spec files; OpenAI writes a unique brief per call; deterministic code for money and state.
What did Hanish build? Engine, brain prompts and schemas, specs, eval harness, dashboard (see HONEST_NOTE).
Where does it fail? (fill from eval: e.g. heavy-accent Telugu numbers, callers who give ranges)
What evidence shows it helps? (fill from eval/results.md)

90-second demo: open in the user's situation (banner photo on WhatsApp) → confirm → judge's phone rings → judge quotes a price → WhatsApp result with evidence quote → dashboard shows the task log → one eval number → one honest limitation.  