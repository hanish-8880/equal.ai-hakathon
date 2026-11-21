# HONEST NOTE

| Component | Status |
|---|---|
| Orchestration engine (spec runner, state machine, confirm gate, retry, idempotency) | BUILT by me — LIVE |
| Specs: dealer_quotes | BUILT — LIVE (1 dealer verified on a real call; parallel verified in tests + simulated) |
| Specs: banner_rate | BUILT — tested with mocks only (NOT yet live) |
| Brain prompts, schemas, router/brief/extractor/reply | BUILT by me (on OpenAI gpt-5.5 / gpt-5.4-mini) — LIVE |
| Dashboard (tasks, calls, briefs, transcripts, evidence) | BUILT — basic |
| Eval harness | NOT BUILT |
| warranty_email, passbook_explain, appointment | NOT BUILT |
| Phone number, voice, live call execution | REUSED — OmniDimension (one generic agent, per-call brief injected) |
| WhatsApp transport | REUSED — Twilio sandbox |
| Transcription, vision, TTS | REUSED — OpenAI APIs |
| Storage | Local JSON-file store (same interface as planned DynamoDB) — DynamoDB NOT yet connected |
| Scheduler | In-process timer for retries — EventBridge NOT yet connected |
| Public URL | Cloudflare quick tunnel (temporary) |
| Test calls / mock banner / mock passbook | SIMULATED inputs, played by consenting people (me so far) |
