You read the transcript of a phone call TO Chinku (the AI assistant of a plumber). Return JSON only.

First decide caller_kind:
- "customer": someone asking for plumbing work / booking a visit (even if they use the plumber's phone to test).
- "plumber": the plumber himself giving Chinku a task (create job, call a shop, invoice, status...).
- "unclear": neither is clear.

For a customer, extract each field as {value, status, evidence_quote}:
- status: confirmed (clearly said and agreed), tentative (said but unsure, or only a bare "yes" to Chinku's suggestion),
  refused, not_mentioned, conflicting (said two different things). evidence_quote = the caller's exact words from the transcript.
  No quote → not_mentioned with value null. Never invent a value.
- caller_name, issue (short, e.g. "kitchen tap leak"), site_address (building/area), flat (flat no. + floor), access_notes (who opens).
- chosen_slot_id: the id (S1, S2…) from open_slots of the slot the caller ACCEPTED in the end. Only a slot from that list.
  If they accepted a time that is not in the list, or did not accept any → value null.
- preferred_time_text: if they asked for a time not in the list, their words (else null).
- emergency: true only if flooding / burst pipe / water not stopping.
- readback_confirmed: true only if Chinku read the booking back and the caller said yes.

Transcript lines: "assistant:" = Chinku, "user:" = caller. Times are IST.
