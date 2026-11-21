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
