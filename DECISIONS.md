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

## [Sat 15:30] DEPLOYED to AWS — https://16-145-161-18.sslip.io (VERIFIED)
Observed: workshop identity = assumed-role/WSParticipantRole. iam:CreateRole + PassRole ALLOWED → instance role chinku-ec2 attached.
EC2 i-0ac92410a0ded5cfa (t3.small, us-west-2), Elastic IP 16.145.161.18, Caddy auto-HTTPS on sslip.io → /health 200 over HTTPS.
Server .env has 0 AWS keys; STORE_BACKEND=dynamo; DynamoStore write via role OK. Tables chinku_{tasks,calls,contacts,events,seen} (on-demand).
Both Omni agents' webhooks → https://16-145-161-18.sslip.io/webhooks/omni. Local server + Cloudflare tunnel retired.
Note: local JSON data (old tasks, contacts) was NOT migrated — contacts must be re-added on WhatsApp.
Deploy after each commit: git push && python -m scripts.aws_deploy push

## [Sat 16:30] Feature A: call_from_document (call every number on a poster / insurance paper / PDF)
Chose (Hanish approved order: A now, eval next, B=Gmail→analyze→PPT after eval):
- Perception lists every phone number with its label (image fields.contacts / DocRead.contacts); code copies them into `targets` (router never retypes digits).
- Router writes 2-5 questions {field, question} + purpose from the user's words.
- Generic engine change: call step may use `must_collect_from: inputs.questions` (dynamic fields → brief + extraction schema); `plan_text` for the confirm message.
- Numbers that fail validation (landlines, partial) are SKIPPED and listed in the plan, not blocking.
Observed (live vision + router, simulated calls): mock insurance letter → 3 numbers with labels; landline skipped; questions: cover, premium payment, claim process.
Gave up: calling landlines (indian_mobile validation only).

## [Sat 16:50] Feature B: email_pdf_report (Gmail PDF → analyse → WhatsApp value → PPT) — new step types fetch_email, make_doc
Chose (Hanish approved Gmail read of his own inbox): IMAP read-only (select readonly + BODY.PEEK, nothing marked read/moved), same app password.
Newest mail with a PDF in last 7 days, optional sender/subject hint (contacts can hold "boss" email). Raw PDF never stored; only DocRead.
Totals via existing compute step (skip_if_empty for documents without line items). PPT: brain writes outline (SlideDeck), python-pptx renders;
item table + total come from code. File served from /media and sent on WhatsApp.
Observed (LIVE Gmail + brain, simulated WhatsApp): self-sent test mail found in 3,554-message inbox; ₹64,965 computed = printed; reply in 24 s;
7-slide deck in 44 s with exact item table. Defects fixed: router left make_ppt false (added input hints + boolean rule);
email date in US time → IST; deck bullets leaked tool instructions → audience rule in write_deck prompt.

## [Sat 16:45] BLOCKER: Twilio trial = 50 WhatsApp messages/day (error 63038, HTTP 429)
Observed: after 50 outbound messages today, every reply fails with 63038 "exceeded the 50 daily messages limit". Inbound still arrives; tasks ran but replies were lost, and the send error crashed the task.
Chose: send failures are non-fatal (logged as wa_send_failed; reply stays in task state + dashboard); removed 2 acknowledgement messages
("calls start chestunna", "call lo cheppindi ardham ayyindi"); voice note + text sent as ONE message.
Real fix needs Hanish: upgrade Twilio (add balance, removes trial cap) or a second Twilio account for the demo. Decision pending.

## [Sat 16:55] Inbound call mis-hearing ("Mr. Casual mentor") — causes + fixes
Observed (call 11:18 UTC, 59 s): ASR fragments ("Can you.", "Sent a WhatsApp.") and garbled phrases; the agent confirmed garbled
content as a task and accepted an unsupported one ("reply to his mail"). Outbound Telugu calls were clean.
Cause 1: inbound silence_timeout = 200 ms (outbound 400) → utterances cut at short pauses. Fix: 700 ms (VERIFIED in agent config).
Cause 2: no ASR context terms. Fix: asr_context_terms = Chinku, Hanish, WhatsApp, PDF, PPT, mail, Suresh, mummy, invoice, ... (VERIFIED).
Cause 3: prompt let the agent repeat garbled words as facts. Fix: inbound prompt — if it sounds wrong, ask again (max 2) then
"WhatsApp lo type cheyyandi"; never guess names; emails via WhatsApp; explicit capability list; unsupported → say so.
Not fixable via API: Soniox language order stays [en, te, hi] (outbound has te first). Temperature 0.3 → 0.2.

## [Sat 17:05] Inbound intake safety: WhatsApp "yes" for every call-taken task; router must not force a spec
Observed: garbled call → router forced "reply to Mr. Casual Mentor" into email_pdf_report (failed harmlessly). Re-transcribing the Omni
recording with gpt-4o-transcribe showed the caller's voice is almost absent in the recording (only "Yeah. Okay") → weak/faint caller audio,
not only ASR settings. Chose: every task from an inbound call waits for a WhatsApp yes (shows what Chinku understood); router returns
null + "can't do that yet" for requests that match no spec. Gave up: re-transcription of recordings as the intake source (caller audio too faint).

## [Sat 17:15] Twilio upgraded → Full account, $20 balance (VERIFIED: type=Full, test message delivered)
50/day trial cap removed. Sandbox still used (judges must send the join code once).

## [Sat 17:25] Router over-correction fixed; routing on gpt-5.5; new spec call_and_ask
Observed: after the "never force a spec" rule, gpt-5.4-mini refused valid requests ("Call him for chair price 99…", "Book alinement").
Chose: rule matches on ACTION (choose the spec, ask for missing details; null only when no action fits); dealer_quotes covers ANY item
(default unit "piece"); new generic spec call_and_ask (call given numbers/contacts, questions from the request); router model → gpt-5.5.
Observed (LIVE): 10/10 routing cases correct incl. both refusals for unsupported (reply to mail, book cab); avg 4.7 s per route.
Gave up: ~3 s per message vs gpt-5.4-mini. Would change if: latency complaints → few-shot examples on the fast model.

## [Sat 17:40] Twilio sandbox URL keeps flipping back → web chat fallback channel
Observed: sandbox inbound URL reverts to the old trycloudflare address ~5-15 min after each save (3:45✅→3:59❌, 5:09✅→5:13❌, 5:18❌).
Checked: no process on the laptop (no python/uvicorn/cloudflared/ngrok); no code in the repo or git history writes Twilio config (Twilio has
no sandbox-URL API anyway); PUBLIC_BASE_URL = AWS in local + server .env. → Twilio-side behaviour, not ours.
Chose: /chat web page (secret CHAT_KEY in URL) on the same engine and the SAME owner identity (contacts, pending tasks, confirmations shared).
Every outgoing message is mirrored into the web chat; WhatsApp is sent only if the owner's last message came from WhatsApp.
Photos/PDFs upload in the page; voice notes and PPTs render inline. Calls are unchanged (real Omni calls).
Gave up: nothing on WhatsApp — it still works whenever Twilio routes correctly.

## [Sat 17:35] Twilio routes inbound inconsistently → poll Twilio's message log as a backstop (VERIFIED misrouting)
Observed: "hi" 17:29:53 → AWS (replied); "Call him for chair price…" 17:30:16 (23 s later) → old trycloudflare URL (11210).
Not time-based flip; Twilio sandbox serves stale config for some requests. Messages still exist in Twilio's log.
Chose: server polls GET /Messages.json?To=<sandbox> every 10 s and processes inbound messages the webhook missed
(claim on wa:{MessageSid} → webhook and poller never double-process; on startup only the last 2 min are replayed).
Cost: up to ~10 s extra latency when Twilio misroutes. Web chat remains the demo fallback.

## [Sat 17:50] English-only for now + voice: Cartesia "Kiara" (Indian-accented English)
Observed: Telugu speech on calls sounded broken ("buffering") — the Hindi-tuned Aarti voice reading Telugu; Hanish asked for English only.
Chose: DEFAULT_LANGUAGE=English (config) → call briefs, agent replies, WhatsApp replies, voice notes, router; all fixed engine messages in English.
Agents always SPEAK English but still UNDERSTAND Telugu/Hindi answers (Soniox te/en/hi kept). Route examples stay Telugu-English (users type that way).
Voice: Cartesia f8f5f1b2 "Kiara" (tag en, Indian accent) on both agents — best English-Indian option with low latency;
ElevenLabs is premium with only one Indian (male) voice; agent LLM stays gpt-4.1 (proven on live calls; only gpt-4o is premium above it).
Would change if: Telugu needed again → set DEFAULT_LANGUAGE and spec language back, choose a Telugu voice.

## [Sat 18:30] Eval v1 (12 cases: 5 real calls + 7 scripted) — honest result: NO accuracy win over the baseline yet
Harness: eval/run_eval.py → eval/results.md/json (generated). Baseline = same model gpt-5.5 + "Summarise this call and list the price and details."
(asked to fill the same field names, for scoring). Scoring bugs that were unfair to the BASELINE were fixed before reading results:
empty answers counted as values; "free"/"no charge" not read as 0; hedge words ("maybe", "to be confirmed") now count as tentative for the baseline.
Labels were written before running and not changed after (real_03 inclusions label debate noted in the case file).
Result: field accuracy Chinku 43/44 vs baseline 44/44; wrong-confirmed 0/11 vs 1/11; Chinku status accuracy 42/44.
Meaning: on clear transcripts a plain gpt-5.5 prompt reads values as well as Chinku. Do NOT claim "more accurate".
Defensible claim: Chinku returns machine-usable status + evidence per field, which drives the confirm gates, deterministic ranking and
"(not confirmed)" marks; the baseline returns prose. Would change if: harder REAL role-play calls (noisy, mixed language, ranges,
conflicting answers) show a gap — that is the next eval step (needs volunteers).

## [Sat 18:45] Conversation-state bugs from a live run (banner → "ask for price" asked for dealer numbers repeatedly)
Observed: banner photo, then "ask for price" → router chose dealer_quotes (not banner_rate) → asked item/quantity/dealer number one by one,
ignored the number on the photo, then asked the builder about "delivery and transport" for a flat. An old dealer task from 17:35 stayed
"running" (no webhook) and old planned tasks kept capturing new messages.
Chose: (1) router — attached image type decides the spec (banner+price → banner_rate, never dealer_quotes for property);
(2) phone numbers read from a photo fill list inputs (dealers) deterministically; (3) pending tasks expire after 30 min of no activity;
(4) CLAUDE.md §9 timeout implemented: dispatched call with no webhook for 10 min → poll Omni log once (same idempotency key) → else failed,
step finishes and the user gets the report; (5) last Telugu hint removed from dealer_quotes.
Verified live: banner photo + "ask for price" → banner_rate plan with the banner's number, no extra questions.

## [Sat 18:55] Retries persisted (were in-memory timers lost on every deploy)
Observed: chair task t_e4b4ac9a stuck "running" 40+ min: one call no_answer → retry scheduled with threading.Timer → server redeployed →
timer lost → step waited forever. Chose: the no-answer call record becomes status "retry_scheduled" with retry_at (DynamoDB);
the 15 s loop fires due retries (idempotent claim retry:{call_id}); a retry_scheduled call keeps the step non-terminal.
Stuck task closed manually (status failed, note). Remaining in-memory timers: wait_for remind/timeout only (logged as a limit).

## [Sat 19:40] PIVOT adopted: Chinku for Plumbers (new CLAUDE.md; v1 archived in docs/CLAUDE_v1_errands.md)
Chose: a persistent Job (app/jobs/models.py, CLAUDE.md §5) on top of the existing engine. Plumber router (gpt-5.5) returns
{intent, job_ref, entities, missing, clarifying_question}; job resolution in code: named job → that job; none named + one open job → it;
several open → ask (never guess). Each plumber action will be a spec bound to a job; v1 features still work via "other" intent.
Built (LIVE-tested with the real brain, in-memory store): create_job from the §6.1 example → J21, issue/flat/site/WO/billing with evidence,
plumber windows resolved to ISO (tomorrow 11–13, 16–19), next action "Call the customer to book a visit"; status query; several
customer numbers → asks which; missing windows → asks only for windows. Job ids start at J21 (JOB_ID_START) so the demo can reach J24.
Chinku App (/chat?key=): job chip, job cards, hold-to-talk mic (MediaRecorder → upload → gpt-4o-transcribe), "🎙️ heard:" transcript,
quick replies, text-labelled status chips. Language conflict: new CLAUDE.md §8 says Telugu-English replies; Hanish asked for English
earlier today → kept DEFAULT_LANGUAGE=English (router still understands Telugu-English). Flagged to Hanish.

## [Sat 20:45] §3A listening pipeline — transcription model, normaliser, understand + gate (VERIFIED pieces)
Transcription (16 s synthetic Telugu-English plumber note, gpt-4o-mini-tts → models, vocabulary prompt on/off):
gpt-4o-transcribe + vocabulary prompt was best: "రెండు యాంగిల్ వాల్వ్స్ … ALD-0517 … మూడు వందల యాబై"; gpt-transcribe spells numbers
("ట్వెంటీ ఫోర్", "zero five one seven"); gpt-4o-mini-transcribe garbled. All mis-heard "4 to 6" in the synthetic clip → numbers always "check".
Chose OPENAI_MODEL_TRANSCRIBE=gpt-4o-transcribe with per-request vocabulary (domain + plumber's names/shops/jobs/WOs/models) and logprobs
(low-probability tokens → whole-word "check" highlights). First synthetic clip was broken TTS (0.36 s) — detected and redone.
Normaliser (code, 24 unit tests): Telugu script + romanized + English + Hindi number words, "two fifty"=250, lakh/crore, ₹/units,
self-corrections (last value wins), job refs, phones, model codes protected (ALD-0517 never becomes 517); ambiguous short words
("do", "char", "teen") are numbers only next to a number word/unit.
Understand: structured multi-action UnderstandResult (intent, job_ref, entities, missing, confidence) + readback + one clarifying question.
Gate in code: confidence < 0.8 / missing / ambiguous job → ask (no state change); consequential intents → readback + wait for yes
(haa / avunu / yes / pampu / అవును); else act, reply starts with the readback. Corrections update only that field.
Latency: understand on gpt-5.5 default 14.6 s vs reasoning_effort=low 3.7 s with the same actions/readback → low for understand only.
End-to-end on the synthetic note: 2 actions in order (source_part 0.92, conditional ask 0.62) → gate = ask → readback + "Did I get that right?".

## [Sat 21:00] Chinku App UI v2 (smooth, mobile-first)
Chose: single-file vanilla app (no build step): live status line (Online / Listening… / Understanding… / Thinking…), optimistic bubbles
(text, file, "🎙️ Voice note · 0:07") replaced by the server echo / "Chinku heard" bubble, typing indicator, slide-in animations,
hold-to-talk with live waveform (AnalyserNode), timer, slide-left-to-cancel, haptics, 90 s cap, welcome screen with tappable examples,
context quick replies (Yes / No / Change / Jobs), jobs bottom sheet with per-job timeline, read-aloud toggle (speechSynthesis en-IN,
remembered per device), dark mode, safe areas, reduced-motion support, add-to-home-screen metas. Polling 0.8 s while waiting, 2.5 s idle.
JS syntax-checked with node --check; real-phone check pending (Hanish).

## [Sat 20:10] Chinku App v3 — colourful theme (docs/ui-theme.png) + language onboarding
Chose: lavender #EEEAFB background, violet hero, bento colours by meaning (visit mango, quote sky, approval pink, bill mint, decision coral),
ink #15131F text on every pastel (contrast), white 70% status pills that always say the word, Plus Jakarta Sans + Noto Telugu/Devanagari,
26px cards / 28px buttons / pill chips, soft shadow, CSS/SVG pipe/valve/wrench/bill shapes (no photos/copied art), staggered fade-up,
press scale .98, flash on value change, reduced-motion respected. Screens: Today (hero = most important real item, week strip with
visit dots + day filter, bento), Jobs (filter + timeline), Calls (real calls, fields with status + evidence quotes, transcript),
Bills, Chat (ink/white bubbles, violet waveform + highlights). Floating ink nav with white mic circle → chat.
Language onboarding on first run (English / Telugu / Hindi / Telugu+English) → stored per plumber; used for readbacks and replies
(calls stay English). Light theme only (reference is light; Hanish's PC is in dark mode and the old dark variant hid the theme).
Render-first (no blank page while the profile loads). Verified by screenshots with true mobile emulation (CDP, 390×844 @2x) + desktop 1280×800.
Real-data rule: all cards come from the job/call store; empty states otherwise; nothing simulated is shown (Calls marks "Live call").

## [Sat 20:05] INCIDENT: a test dispatched a REAL Omni call to a sample number (+91 98480 12321)
Observed: after book_visit went live, test_consequential_waits_for_yes_then_runs reached runner.start_spec → steps.DISPATCH (not mocked)
→ Omni dispatched a real call (log 14:35:17 UTC, task t_56561877) to the test fixture's sample number. Omni: no-answer, 0 s — not
connected. No retry possible (retry state lived only in the test's in-memory store; AWS has no such task).
Fix: tests/conftest.py autouse guard — Omni dispatch, OpenAI client, WhatsApp send and SMTP send RAISE in every test unless the test
mocks them explicitly. Lesson: every outward action must be blocked by default in tests, not mocked per test.

## [Sat 20:10] Call prompts per use case + booking with live slots
Chose: role-specific call guides app/brain/prompts/calls/{client_booking, shop_part_check, approval_request, client_reschedule,
visit_confirm}.md appended to the brief writer by step.call_kind; every brief ends with a numeric readback. Base agent persona
"on behalf of {{on_behalf_of}} (an independent plumber…)".
Chose: new step type job_op (app/jobs/ops.py: book_slot, notify_client, visit_confirmed) — deterministic code writes call results into
the job with evidence + call id. Booking spec book_visit (internal, started from a job after the readback gate): booking call (only
windows still free = live availability) → book_slot (conditional-write lock slot:{plumber}:{start}; taken → offer next free window;
other time asked → visit pending, plumber decides) → WhatsApp confirmation if the customer has joined the sandbox (tracked from inbound
messages) else recorded as call-only → schedule → visit_confirm call 1 h before (60 s with DEMO_REMINDER_SECONDS) → result to plumber.
Tests: slot lock conflict, evidence on booking, pending on other-time, past windows never offered, gate → booking call (82 passing).

## [Sat 20:40] Parts sourcing live: OSM shops shown, plumber's shops called, offers compared in code, dependency rule
Observed (OSM, Kondapur): 10 hardware/sanitary shops within 4 km, 0 phone numbers, Overpass ~32 s → shops are SHOWN (address, distance,
map link, background card); calls only to shops in the plumber's contacts or numbers he gives (Hanish's choice: "call only on my pick").
Built: part_extract (model EXACT or ask — never guessed; shops; rule "Srinivas ≤ ₹500 exact → ask customer 4–6"), part_sourcing spec
(shop_part_check call per shop: exact model, stock today, units, selling price, MRP, GST, delivery, condition, pickup, hold),
record_offers (evidence + call id; newer call supersedes older offer), rank_offers (exact + in stock first; unknown delivery → total
unknown; conditional price only if condition met), dependency check (met → next action book; not met → reasons, visit pending, ask
plumber: wait / take other model / another shop), offers + nearby-shops cards in the app. Bug caught by tests: ₹0 delivery treated as
unknown (0 == False) → fixed.

## [Sat 21:00] Price list, estimates, invoices, customer WhatsApp receptionist
Chose: DEMO price list (clearly labelled on cards + PDF; plumber's own list in prefs overrides). Spoken rates always win.
Invoice (§6.6): LLM lists lines only → code prices (spoken rate > price list), excludes returned parts, reconciles approvals
(unapproved / different amount flagged) and spoken total, computes total → readback → "yes" → number + PDF (fpdf2) → idempotent
sends (claim per number/key/channel/recipient): email to billing contact (demo override → own inbox), WhatsApp to customer if joined.
Drafting acts without the understanding gate; SENDING always needs its own yes. Corrections re-draft from the previous draft.
Estimate: photo reading (visible_problem, parts_visible) → estimate_extract (physical parts, labour hours, urgency, questions) →
price list → card "Estimate · Demo rates" → yes → customer. Customer WhatsApp (sender ≠ plumber): known customer photo → estimate for
their job + plumber card; text → forwarded; unknown → receptionist reply (+ "turn off the main valve" on emergency words) + lead alert.
Today hero: "Visit now · … — Finish & invoice" during/just after a booked slot.
VERIFIED LIVE (real brain): CLAUDE.md closing note → "2 angle valves ₹700, 1 hose ₹300, labour ₹400. Total ₹1,400. Not billed: 1 ×
angle valve (returned)" in 7.5 s. Leak photo → problem read correctly → estimate; fixed double-counting (service line + labour).
Tests: ₹1,400 case, spoken-total conflict, unapproved extra work, idempotent send, receptionist emergency reply (91 passing).

## [Sat 21:20] INCIDENT: broken deploy took the server down (~5 min) — deploy is now gated
Observed: a syntax error in assistant.plan_lines; pytest failed at collection, but my shell chain still ran git commit + aws_deploy
(the commit/deploy line was not chained to the test result) → chinku.service crash-looped; HTTPS check failed.
Fix: syntax fixed; scripts/aws_deploy.py push() now runs the full test suite first and ABORTS on any failure, and after restart polls
/health and exits non-zero if the server is not healthy.

## [Sat 21:40] Today type bar, caller number on inbound, customer WhatsApp never held back, sample bookings, owner E2E script
Observed bug: customer messages used deliver(), which skips WhatsApp when the recipient's last message came from the app — so when
the customer number == the plumber's number (our tests), the customer never got WhatsApp. Fix: notify_customer always sends WhatsApp
directly and logs wa_out_customer. Inbound call confirmation now starts "📞 Call received from ******3607 — here's what I understood".
Today screen has a "Type or talk to Chinku…" bar (typing was only in the chat screen). Job.sample flag + "Sample" chip; seed script
uses the owner's own number for every sample customer (no button can call a stranger). scripts/e2e_owner.py: full flow as the owner,
real WhatsApp/email/maps/PDF, scripted (simulated, 🧪-marked) call answers; fires the confirmation-call schedule itself so the server
never rings the phone during the test.

## Sat 22:10 — Customers can call in and book (inbound, customer side)
- Before: inbound calls from any number except the owner were ignored (owner-only task taking). The inbound agent still had the v1 errands prompt.
- Now: one inbound agent, two modes (customer receptionist / plumber task). Free slots = WORK_HOURS (default 10–12, 12–14, 16–18) for SLOT_DAYS=3 minus locked/past slots, computed in code and pushed into the agent prompt (startup, every 5 min if changed, after every booking). VERIFIED live: agent prompt shows the 4 real free slots.
- After the call: brain reads the transcript (caller_kind, fields with evidence, chosen slot id); CODE maps the slot to a real window and books only if read back/confirmed AND the conditional-write lock wins. Otherwise pending + plumber asked. WhatsApp confirmation only if the caller's number joined the sandbox.
- The owner's number can test as a customer (routed by what was said; tasks are still accepted only from the owner's number). 5 new tests (98 total).
- UNVERIFIED until a real customer-style call is made.

## Sat ~23:00 — Languages for real (te / hi / te-en)
- App UI: every label, chip, empty state, toast and date in the chosen language (dictionary in app.html; `&lang=xx` = view-only override for demos).
- Chinku's messages: translated at the single outbound point `deliver()` by the fast model. CODE GUARD: the multiset of digits must be identical and the "(yes …)" answer hint must survive, else retry with the brain model, else the English original is shown. Cached per text (tr3:). VERIFIED live on the ₹1,400 readback in te / hi / te-en (all numbers exact). First attempt was too timid (left "Total", "labour" in English; Hindi transliterated "Sai Facility"; "against WO-117" → "के खिलाफ") — fixed with explicit rules + per-language examples.
- Jobs API translates next steps in the background (never blocks the screen; first version blocked ~20 s).
- Yes/No: Hindi words added (haan, theek hai, bhejo, nahi, हाँ, नहीं…); app quick-reply buttons show the language but send "yes"/"no".
- NOT translated (honest): phone calls (English — Telugu voice was choppy earlier), customer WhatsApp messages (customer's language unknown), invoice PDF (English for the billing company; the PDF font has no Telugu), job data the plumber/customer said (issue, address) and timeline evidence.
- Incident: a dropped 22 s customer call created job J33 ("application"). Rule now: job only if problem AND (address or agreed slot); else "Missed enquiry" card. J33 cancelled (kept) with the reason.

## Sat 23:55 — Pivot to Chinku (CLAUDE.md FINAL) — checkpoint "Sun 1:00 AM"
- Observed: planner (gpt-5.5, effort low) turned "Transcribe this, email me a copy, and remind me tomorrow at 9" into transcribe_image → draft_email(exact_copy) → send_email[approval] → remind(Tomorrow, Sun 22 Nov, 9:00 AM). Live: transcript exact vs ground truth (font page), email delivered (IMAP verified), reminder stored.
- Chose: tools wrap existing code (email, web search, maps, call engine via call_and_ask spec) — no engine rewrite. Approval enforced in CODE for send_email/call even if the plan forgets (test). Transcripts go by email EXACTLY (mode exact_copy), never rewritten.
- Chose: tasks/memory/reminders live in the existing `contacts` table (itask:/mem:/rem:/ilast:) — no new DynamoDB table needed (EC2 role is limited to chinku_* tables).
- Fixed on the way: memory secret filter blocked +91 phone numbers (12 digits looked like Aadhaar) → now 4-4-4 format or the word Aadhaar/UID.
- Times: planner proposes ISO from now_ist; code validates (future, IST) and renders words. Would change: a code date parser for common phrases.
- MODE=chinku is now default; plumber flows stay with MODE=plumber.

## Sun 00:40 — Plumber removed; Chinku App live
- Removed (git history keeps it): app/jobs (jobs, booking, parts, billing, customer calls, slots), plumber prompts/call scripts, book_visit/part_sourcing specs, plumber tests and scripts, old UI. Kept: engine + v1 recipes (CLAUDE.md: still selectable), normaliser, translation, voice transcription (now app/brain/listen.py with a vocabulary from the user's memory).
- Omni agents renamed to Chinku (welcome + prompts pushed by scripts/omni_sync_agents.py, read back OK). Inbound = owner tells Chinku what they need → same planner + approval.
- New Chinku App: home + chat cards + Leaflet map + 🔊 Listen (/api/tts, cached). Observed: J1 via the app — email arrived with the photo attached (IMAP: att_….jpg, 107 KB).
- 97 tests pass (plumber tests removed with the code).

## Sun 01:30 — Swiggy MCP login test + Kiwi.com flights LIVE adapter
- Swiggy (Observed): discovery live; DCR POST /auth/register → 201 (shared client_id "swiggy-mcp"); phone+OTP on Swiggy's page; token 200. /food 20 tools, /im 17 tools. Real results: search_restaurants("veg lunch") → 3 restaurants (literal match, so J9 must query food words like "veg meals"/"thali"); search_products("soap") → 20 products with offerPrice/MRP/unitLevelPrice; my per-100 g in code matched Swiggy's. Read-only script (cart/checkout/payment blocked in code), tokens in %USERPROFILE%\.chinku (outside repo). Docs say production is "invite-based" → keep demo fallback. Gave up for now: menu prices / final payable (cart tools) — 9:30 AM checkpoint.
- Kiwi.com (Observed): https://mcp.kiwi.com Streamable HTTP, no auth, tool `search-flight` (dd/mm/yyyy, city names resolved, currency param). Real: Hyderabad → Bengaluru Fri 27 Nov, 1 adult → 15 itineraries in INR. Booking link resolves to kiwi.com/en/booking (HTTP 200).
- Chose: adapter layer app/chinku/adapters (Offer, ProviderAdapter.search/book, rank()). Ranking is code (cheapest/fastest/earliest; unknown price ranks last, never ₹0; failing constraints excluded WITH reason). book() only returns the provider link — Chinku never pays. Fixture = real saved Kiwi response (tests/fixtures/kiwi_hyd_blr.json); 6 adapter tests.
- Would change: Kiwi price = provider's displayed total; bags/service fees can change at checkout → card says "price as shown by the provider".

## Sun 00:00–01:00 — "Build all the stuff": J3, J7, J2, J10, J11, J9 rides, J5 fallback, eval
- J3 (Observed): find_support on Bajaj → consumercare@bajajelectricals.com, source bajajelectricals.com → official (code: brand in domain; aggregators excluded). Complaint drafted with [purchase date]/[model] placeholders (never invented). Bug found live: a text-only request re-used an old photo (30-min reuse) → now reuse only when the message refers to a photo.
- J7 (Observed): electricity-disconnection SMS → Likely scam (cybercrime advisory). Code rule: ≥2 red flags can never come back "Looks genuine".
- Promises: commitments heard on calls become tracked promises ONLY with an exact quote found in the transcript; follow-up fires once; "no" → escalation ladder (helpline → email → grievance officer → National Consumer Helpline 1915).
- Calls: per-call language (Telugu for Mummy, Kannada shop); outbound agent now has Kannada + Tamil (read back). Call result card: "Booked" only when a booking field is CONFIRMED (code). Reminder CALLS need approval. Night rule: 21:00–08:00 IST no calls to unsaved numbers — checked BEFORE asking approval.
- J2 (Observed): search → 6 real clinics with phones + official sources; OpenStreetMap could not geocode full Indian addresses or names → fallback to locality, pins labelled "approx. area"; 1 req/s Nominatim policy.
- J11 (Observed): Araku 28–29 Nov plan, 6 hotels + 6 restaurants ranked by rating in code; only real mobiles are callable (bug caught by test: "0891-2345678" landline looked like a mobile after dropping the 0). Web search raised to 180 s + low reasoning effort (first run timed out at 60 s).
- J9 rides: Ola/Uber/Rapido DEMO adapters (real distance, demo fares). Observed: Madhapur → Kukatpally 8.1 km, Rapido Auto ₹122 recommended. Arrive-by compares real times (text compare was fragile).
- J10: tap-to-talk with silence auto-stop + Talk mode (auto-spoken replies). Not tested on a phone yet.
- J5: live transfer not switched on → fallback card (CLAUDE.md).
- Swiggy: tokens moved to the server (~/.chinku, chmod 600); laptop copy parked because refresh tokens rotate.
- Eval (Observed): transcription 0.8% vs 17.5% WER (same model, plain prompt; baseline mostly ADDS text/translations); planning 22/22, 0 wrong actions (builder-written); restart persistence pass. PIL could not shape Telugu (no raqm) → Telugu pages rendered by Chrome. Removed a test assertion that could never fail ("… or True").
- Editing pitfall hit again: \b written as backspace chars in a regex (filter silently never ran) — fixed; a fair test now catches it.

## Sun 01:15 — Food + Dineout complete, toll-free calling, ChatGPT-style chat
- Swiggy Food (Observed live): search_menu("veg meals") at the saved address (Visakhapatnam) returned only 3 dishes, all > ₹250 at 1 AM → food_search now tries up to 4 dish names and filters/ranks in code. Result: Veg Biryani · Hotel Prabha · ₹110 · 35–40 min · 4.6★ (+3 options; 5 excluded with reasons). Added to the real food cart after "yes": update_food_cart needs addressId (not in its schema's required list). get_food_cart → data.pricing: item ₹110 + delivery ₹133 + taxes ₹37.48 = to pay ₹280; delivery_issue "Restaurant is closed" + item out of stock → shown as a warning. place_food_order / payment tools stay blocked.
- Swiggy Dineout (Observed live): search returns TEXT (parsed by regex: name, ★, ₹ for two, km, ID); get_available_slots details are in the text part (structuredContent is a summary) → adapter now keeps both. table_check validated Mon 23 Nov 8:00 PM inside the Dinner window and picked the FREE itemId; approval card shown; NOT booked (real reservation — the user decides). Paid pre-book deals are shown, never booked.
- Toll-free: validate.phone_kind → mobile / landline (0 + STD + separator) / tollfree (1800/1860/1600); spec validator 'dialable'. Night rule exempts toll-free helplines only. Observed: Bajaj's official number is a Mumbai LANDLINE (022-41280000) → blocked at 1:13 AM before approval, with the "call them at 10 AM" option. A real helpline call is still to be made in the daytime; IVR keypad menus remain a limit (said in cannot_do).
- UI: one chat screen (assistant text without bubbles, user grey bubbles, pill composer, welcome suggestions); approvals are buttons inside the chat; drawer for Needs you / reminders / recent / memory & settings. Built by reusing the proven JS blocks (cards, voice, polling) — scratchpad backup of the old file kept during the change.

## Sun 01:45 — Hardening pass ("everything solid")
- REAL BUG: every approval button sent the word "yes", which approved the NEWEST waiting request — tapping "Call now" on an older card could approve a table booking. Now: /api/approve(task_id, step_id) → executor.approve() answers exactly that request; idempotent (second tap / old card → "already decided"); old cards are matched to their waiting task by content; decided cards show "Already decided". A bare "yes/no" with nothing waiting → "Nothing is waiting for your answer" (but "send it" is still a request).
- Live regression (scripts/regression.py, 18 features, real services, test identity, outgoing actions answered "no"): run 1 → 17/18 (compute gave the answer silently → fixed: compute/recall/extract always show results). Run 2 → 17/18 (compute fixed; recipe → Instamart failed with HTTP 429).
- Swiggy 429 (Observed): after tonight's many runs Swiggy rate-limits even `initialize`, with no Retry-After. Chose: cache read-only lookups 20 min, 2 short retries, then a 2-minute cool-down with a plain message (never hammer). Would change: ask builders@swiggy.in for limits before the demo; rehearse with cached results.
- UI button test (CDP, live): 19/19 — never taps a real approval or "New chat".

## Sun 01:40 — Multiple Swiggy accounts + a silent deploy failure
- Chose: several Swiggy logins (each its own phone + OTP), one active (Settings → Swiggy account). Tokens per account in ~/.chinku/swiggy/<name>/ on the server (first login stays "main"); sessions, cache and 429 cool-down are per account. Login: `python -m scripts.swiggy_login_test --account <name>` (private window, no test searches), then `python -m scripts.swiggy_upload <name>`. Warning shown in Settings: for a real second person, not to get around Swiggy limits (their terms allow revoking credentials for token abuse; the limit may also be per client/IP).
- INCIDENT (Observed): a deploy printed "health OK" while the server still ran the previous commit — `git pull` refused to overwrite eval/regression.json (written on the server by the regression run, then committed from the laptop). Fixed: deploy does `git fetch && git reset --hard origin/main` and then verifies the server's HEAD == laptop HEAD, failing loudly otherwise. Checked: only that one deploy was affected (server was on the previous commit 3007df2).
