# Submission text — Chinku

**Project:** Chinku — tell it what you need, and it gets it done.

**What are you building?** Chinku is an everyday assistant with one question: "What do you need?" You type, speak in
Telugu, English, Hindi or a mix, or show a photo. Chinku plans the steps and does the work: it reads photos and documents,
searches the web, makes real phone calls with an AI voice agent, emails anyone, sets reminders, and follows up on promises
until the task is done. It finds the official support contact for a broken product and drafts the complaint; it checks
whether an SMS is a scam; it plans a trip and calls the hotels and restaurants you pick; it compares live flights
(Kiwi.com) and turns a recipe into a Swiggy Instamart cart. Rides are shown through demo adapters that plug in when a
provider gives API access. Every result carries proof — exact quotes, sources, ticket numbers — and nothing is sent,
called or added to a cart without your one-tap approval. Chinku never pays.

**What exact problem are you solving?** People know what they want, but still have to turn it into a search, a phone
call, an email and a reminder themselves — and then chase it. Indians spent over 15 billion hours on hold to customer
service in a year (ServiceNow CX report), about 30 hours each. Call assistants today handle calls coming *to* you;
call-for-you features abroad are not available in India or in Indian languages. Chinku does the outbound work and the
follow-through, in your language.

**Evidence:** `eval/results.md` — exact transcription 0.8% word error rate vs 17.5% for the same model with a plain prompt
(13 simulated pages incl. Telugu); planning 22/22 with 0 wrong-action plans; call extraction 0 wrong-confirmed vs 1 for the
baseline; live restart keeps pending approvals. Honest limits in `HONEST_NOTE.md`.

**Pitch:** "Equal answers the calls that come to you. Chinku makes the calls, sends the mails, and follows up until it's done."
