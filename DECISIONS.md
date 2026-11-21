# DECISIONS

## [Sat 12:05] Repo root = project folder
Observed: workspace already holds CLAUDE.md; CLAUDE.md shows a `chinku/` folder.
Chose: use the current folder as the repo root (same layout, no extra nesting)  because it is simpler to push.
Gave up: nothing.
Would change if: judges need a monorepo.

## [Sat 12:05] Omni API shape — UNVERIFIED
Assumed: POST {OMNIDIM_BASE_URL}/calls/dispatch, Bearer auth, body {agent_id, to_number, call_context, from_number_id?}.
Placeholder syntax {{call_script}}: UNVERIFIED.
Webhook payload shape: UNVERIFIED.
To verify: `python -m scripts.omni_smoke` → record result here.

## [Sat 12:20] OpenAI models — VERIFIED live
Observed: key lists gpt-5.5, gpt-5.4-mini, gpt-4o-transcribe, gpt-4o-mini-tts (and many more).
Test "bag ki moodu vandala enabhai aidu rupayalu" → gpt-5.5 = 385 (correct, 4.5 s); gpt-5.4-mini = 885 (WRONG, 1.0 s).
Telugu TTS (gpt-4o-mini-tts) → gpt-4o-transcribe round trip: exact Telugu text back.
Chose: BRAIN=gpt-5.5 for extractor/brief/vision/decider; FAST=gpt-5.4-mini for routing only  because the fast model misreads Telugu spoken numbers.
Gave up: lower latency/cost on extraction.
Would change if: eval shows fast model matches brain on Telugu numbers.

## [Sat 12:35] Omni agent created via API — chinku-caller (id 286768)
Observed: Omni key works (GET /agents, GET /phone_number/list → 200). Number +919429492201 = id 8791 (US/voicelink, active on clinic bot 186903).
POST /agents/create with {name, welcome_message, context_breakdown[{title,body}], call_type, transcriber, model, voice, languages} → 200, id 286768.
Stored prompt keeps {{...}} literally; agent `dynamic_variables` = [] (Omni did not auto-register them). Injection still UNVERIFIED.
Chose: create the agent from code (scripts/omni_create_agent.py) instead of dashboard  because the prompt stays versioned in app/brain/prompts/base_agent.md.
Voice: cartesia c7bbe380… (same as Telugu clinic agent). LLM: gpt-4.1-mini (Omni default; live-turn latency).
Gave up: dashboard tweaking.
Would change if: smoke call shows placeholders not filled → fallback per-call agent (CLAUDE.md §4).

## [Sat 13:05] Spec format additions (engine stays generic)
Observed: dealer_quotes needs one call per dealer and a total-price comparison; banner_rate needs callee role from inputs.
Chose: added generic spec keys `for_each` (fan-out over a list input), `callee` (templated name/role), placeholders `{inputs.x}` / `{item.y}`, and on reply steps `rank_by` (safe + - * / expression, deterministic) + `rank_label`. No `if spec ==` in engine.
Gave up: CLAUDE.md's bare `{phone}` syntax (now `{inputs.phone}`).
Would change if: a feature needs a non-arithmetic comparison.

## [Sat 13:05] Engine verified with LIVE brain, SIMULATED call (scripts/sim_e2e.py)
Observed: WA text → route (gpt-5.4-mini) picked dealer_quotes, parsed 2 dealers + E.164 → confirm gate → 2 briefs (gpt-5.5, Telugu-English, AI disclosure in opening) → simulated transcripts → extract (gpt-5.5):
Ravi 385 + transport 2000 (confirmed); Sai "moodu vandala tombhai" = 390, transport 0, delivery TENTATIVE ("evening confirm chesta").
Deterministic rank: Sai 19500 < Ravi 21250 (transport-extra pair flips winner). Reply led with best total, marked "(not confirmed)", quoted evidence.
Chose: keep extraction guard (confirmed without quote → tentative).
Would change if: live Omni transcripts have a different shape.

## [Sat 13:12] Omni context injection — VERIFIED live (requestId 11127484, log 11681947)
Observed: dispatch {agent_id, to_number, from_number_id, call_context} → {"success":true,"requestId":11127484,"status":"dispatched","custom_variables_count":6}.
Agent said: "Namaste, I am Chinku, an AI assistant calling for Hanish." then "Chinku test number four seven two." and ended the call itself (18 s, $0.032).
=> {{on_behalf_of}} filled in welcome message AND {{call_script}} filled in system prompt. Syntax = {{name}}. Values arrive as strings (max_minutes "1").
Call-log row shape (GET /calls/logs?agentid=): call_conversation "LLM: ..<br/>User: ..", custom_variables = our call_context, call_request_id.id = requestId, call_status, call_duration_in_seconds, hangup_reason.
Chose: one generic agent + per-call call_context (primary design, no fallback needed). Correlate by custom_variables.chinku_call_id; also requestId. Polling fallback = omni.fetch_log(requestId).
Webhook payload shape: still UNVERIFIED (needs PUBLIC_BASE_URL); parser accepts both log-row and webhook-style keys.
Would change if: webhook payload lacks custom_variables → correlate on requestId only.

## [Sat 13:12] Twilio — creds VERIFIED, sandbox NOT joined
Observed: account active (Trial). Test send to +91…3607 → failed 63015 (recipient not in sandbox).
Next: user sends "join <code>" to +14155238886.

## [Sat 13:20] Public URL = Cloudflare quick tunnel (not ngrok)
Observed: ngrok needs an account token; cloudflared quick tunnel needs none. URL https://speak-car-facilities-forests.trycloudflare.com → /health 200, /webhooks/omni 200, /webhooks/whatsapp 200.
Omni agent 286768 webhook set via PUT /agents/{id} post_call_actions.webhook → post_call_config 455762 points at {URL}/webhooks/omni.
Gave up: stable URL — it changes on every tunnel restart (then: update .env, re-PUT Omni webhook, update Twilio sandbox URL).
Would change if: tunnel drops during demo → deploy to AWS (stable URL).

## [Sat 13:40] First LIVE loop: WA → route → clarify → confirm → Omni call → webhook → extract → WA reply (delivered)
Observed: task t_7b780cba. Router asked quantity (clarifying Q), confirm gate held, brief → call 11128566 → webhook arrived via tunnel.
Webhook payload VERIFIED: top-level call_id, call_request_id (INT = requestId), call_status, call_duration, hangup_reason,
call_report.{full_conversation ("LLM: ..\n" lines), summary, interactions[], extracted_variables}. NO custom_variables → correlate on call_request_id.
Both calls had zero user speech (idle timeout; retry cut_off after greeting). Extractor → no_answer / cut_off correctly; retry fired once after 5 min; reply delivered.
Root cause suspect: agent ASR = deepgram nova-3 language "en" (English-only); clinic agent that works = Soniox [te,en].
Chose: PUT {"transcriber":{"provider":"soniox",...}} → asr_service Soniox [en,te,hi] (VERIFIED in agent config). Flat asr_* keys are ignored by PUT.
Also: reply prompt must not invent evidence when no data; router fixes item typos ("cents"→cement) and names unnamed dealers.
Would change if: next live call still captures no user speech → check phone audio / Omni test call from dashboard.

## [Sat 13:48] CHECKPOINT 1 MET (late, ~13:45) — first successful live call t_6a73d6cc
Observed: after Soniox ASR switch, user speech captured. Dealer said "It's 714", "7 days", transport "No", "Full advance", confirmed repeat-back.
Extracted all 4 fields confirmed; deterministic total 50×714 = 35700; WhatsApp reply delivered.
Weakness: price evidence_quote was the assistant's repeat-back, not the dealer's words.
Chose: extractor prompt now requires the other person's own words as evidence (shortest line).
Also seen: agent switched to English because the callee answered in English (rule 2 working).

## [Sat 14:15] F2 banner_rate LIVE (t_2e3eac4e) + leading-question weakness
Observed: banner photo → vision (phone, project, ₹65L) → confirm → call → 5 fields with callee-quote evidence → reply. Works end to end.
Weakness: agent asked a LEADING question listing 6 inclusions; callee said "Yeah" → extractor marked all 6 inclusions "confirmed" (wrong-confirmed).
Also unit_type taken as "2 & 3 BHK Luxury Apartments" (banner text) instead of default 2BHK; reply wrote ₹6500000.
Chose: brief_writer must ask open questions only; extractor treats bare "yes" to a listed question as tentative and lists only items the callee named; reply formats lakhs/crore.
Gave up: nothing.
Would change if: eval shows open questions make calls too long.
This transcript becomes an eval case (leading-question "Yeah" → inclusions tentative).

## [Sat 14:45] F3 warranty_email — LIVE (simulated WA input, real vision/search/LLM/SMTP)
Observed: mock product label (gpt-image-2) → vision brand/model/serial (0.99) → photo remembered → instruction routed to warranty_email →
web search (Responses API web_search, gpt-5.5, 10 s) found customercare@ttkprestige.com + source URL → draft with [purchase date] placeholder →
user "yes" → Gmail SMTP (nhanish588@gmail.com app password, VERIFIED) → reply.
Chose: new generic step type `email` (to / to_lookup / goal / shareable) with its own confirm gate (preview → yes / no / type a corrected address).
Chose: EMAIL_DEMO_OVERRIDE=true by default — every mail is delivered to the user's own inbox, subject tagged "[DEMO — would go to X]",
  because sending fake complaints to real companies is not OK. Gave up: proof that the brand receives it.
Chose: a photo sent alone is remembered 15 min and attached to the next instruction (WhatsApp sends photo and voice note separately).
Would change if: user wants a real warranty claim sent → set EMAIL_DEMO_OVERRIDE=false for that one task.

## [Sat 14:45] F4 passbook_explain — BUILT, mocked flow tested, brain parts LIVE-tested
Chose: new generic step type `wait_for` (from / kind / timeout_min / remind_text): task → waiting_input; an image from that phone completes the step;
remind at timeout/2, fail at timeout (in-process timers — lost on restart, honest limit).
Chose: reply step may target another contact (`to`) and send a TTS voice note (`voice: true`, Telugu script via gpt-4o-mini-tts).
Chose: contacts saved by a deterministic command "contact <alias> <phone>" (router then maps "mummy" → phone; router never invents numbers).
Observed (live): mock passbook (gpt-image-2) → vision read all 6 rows exactly (0.99). Brief for explain call: warm, all entries, spoken numbers, balance repeated.
Observed: Omni PUT cannot change tts_cartesia_language (stays "en" after 4 payload variants) → Telugu SCRIPT would be unreadable by the phone voice.
Chose: brief_writer must write all call text in English letters (romanized Telugu). Gave up: native Telugu voice on calls.
Would change if: Omni dashboard lets us set the agent voice language to Telugu.
Privacy: raw passbook image is never stored; only extracted entries go into task state.
Also: removed uvicorn --reload — a reload killed an in-flight WhatsApp request (14:11 voice note lost after transcription).

## [Sat 14:40] F4 live run (t_43d935ef) — worked end to end, 3 real defects found & fixed
Observed 1: first call failed: Omni 429 "concurrent call limit (1)" (our plan = 1 call at a time). Task continued to wait_for anyway; report said "didn't answer".
  Chose: dispatch waits in queue on concurrency_limit (poll 15 s, max 10 min); late failures finish the step via a hook; failure reason "dispatch_failed" reaches the reply.
  Impact: dealer_quotes "parallel" calls are effectively SEQUENTIAL on this plan (honest note).
Observed 2: explanation call — our brief had correct numbers, but Omni's live LLM (gpt-4.1-mini) re-translated into Telugu and CHANGED numbers
  (pension 18,500 → "25,000", electricity 1,180 → "180", LIC 3,250 → "3,200").
  Chose: base agent rule 9 "NUMBERS ARE SACRED" + agent LLM upgraded to gpt-4.1 (temp 0.2) via PUT {"model":{...}} (VERIFIED llm_service=gpt-4.1).
  Would change if: still wrong → send the explanation as pre-written exact sentences and ask the agent to read them verbatim / rely on the WhatsApp voice note (our TTS, exact).
Observed 3: voice note for mummy contained an evidence quote and a question for Hanish. Chose: voice-note mode in reply prompt.
Positive: wait_for + photo from contact + 2nd call + Telugu voice note + report all ran live; mummy's doubt ("passbook number?") was captured; agent refused to invent it.

## [Sat 15:00] Agent voice: Cartesia "Aarti" (female, Indian) — Sarvam rejected
Observed: Sarvam "kavya" via PUT {"voice":{"provider":"sarvam","voice_id":"kavya"}} worked on a test call (11132645) but sounded slow/laggy
(user feedback; LLM latency p50 0.97 s was fine → the TTS was the problem). Same test showed gpt-4.1 now speaks 18,500 and 54,882 exactly.
Chose: Cartesia 9cebb910… "Aarti – Conversationalist" (test call 11132725), user OK'd. tts_cartesia_language stays "en" (not settable via API) → keep romanized call scripts.
Would change if: native Telugu pronunciation needed → set voice language in Omni dashboard.

## [Sat 15:15] F5 appointment + `schedule` step
Chose: generic `schedule` step (at_from {step, field} + offset_min). Fire time computed deterministically from the extracted ISO datetime
(extractor now gets now_ist; "repu podduna padakondu gantalaki" → 2026-11-22T11:00+05:30, VERIFIED live).
Persisted in task state; a 15 s tick loop fires due schedules (idempotent claim "fire:{task}:{step}"); POST /internal/fire/{task}/{step} for EventBridge later.
Chose: report to Hanish BEFORE the wait, short "reminder done" after.
Chose: DEMO_REMINDER_SECONDS=60 in the live demo (SIMULATED time) — nobody waits until tomorrow. Real mode = slot − 60 min.
Gave up: AWS EventBridge for now (no AWS creds configured). /internal/fire has no auth yet — only fires already-scheduled steps (low risk); add a token before AWS.
Bad/unclear time → schedule step fails the task and tells the user (never guesses a time).

## [Sat 15:45] Inbound calling + PDF → total → email (new step type `compute`, approved by Hanish)
Chose: separate Omni agent `chinku-inbound` (id 286801, Incoming, prompt app/brain/prompts/inbound_agent.md): greets, takes the ORDER, asks only for missing details,
repeats the task back, ends. It never does the task on the call. Number +919429492201 attached to it via POST /phone_number/attach (VERIFIED active_bot=286801).
Outbound agent chinku-caller unchanged (dispatch names agent_id + from_number_id).
Chose: post-call webhook with direction=inbound (or bot_id = inbound agent) → caller's "user:" lines + Omni summary → runner.handle_message as if WhatsApp.
SECURITY: only the owner's number (USER_WHATSAPP) can give tasks by phone; others are logged and ignored. Confirm gates still apply on WhatsApp.
Chose: PDFs on WhatsApp → OpenAI file input (gpt-5.5) → DocRead line items exactly as printed (VERIFIED: 7/7 items, stated total 64,965).
Chose: new generic step `compute` (sum of a list field; compare with stated total; Indian digit grouping) — money is never computed by the LLM.
Chose: spec inputs with `media:` are filled deterministically from the perceived file (router never copies documents). PDFs are remembered 60 min (user may call later).
Contacts now store email too ("contact suresh suresh@x.com").
Bug fixed: a message during an email gate re-planned the task and left it "running" → started tasks are never re-planned.

## [Sat 15:40] Hosting: AWS Workshop Studio account → EC2 us-west-2 with stable HTTPS
Observed: Equal's AWS account is a Workshop Studio account: only us-west-2 / us-east-1; credentials are temporary (with AWS_SESSION_TOKEN) and expire.
Chose: one t3.small EC2 in us-west-2 + Elastic IP; HTTPS via Caddy + Let's Encrypt on <ip>.sslip.io (no domain needed) → one stable URL for Twilio + both Omni agents.
Chose: DynamoDB on the server ONLY via an instance role (chinku-ec2, dynamodb:* on chinku_* tables). If iam:CreateRole/PassRole is blocked →
  server keeps the local JSON file store. The server NEVER receives the temporary keys (stripped from the copied .env), so nothing breaks when they expire.
Gave up: Mumbai region (not allowed); EventBridge for now.
Would change if: role creation blocked AND judges want DynamoDB → short-lived demo only.
