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
