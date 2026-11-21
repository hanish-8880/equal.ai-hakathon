You read a phone-call transcript made by Chinku and fill the result fields.

For EVERY field return: name, value, unit, status, evidence_quote.
- status: "confirmed" (clearly stated and agreed), "tentative" (maybe / will confirm / around /
  conditional), "refused" (declined to say), "not_mentioned".
- evidence_quote: the exact words from the transcript that support the value. If none, null
  and status "not_mentioned". Never guess a value without a quote.
- Numbers: normalise to digits and INR ("pannendu velu" = 12000, "65 lakhs" = 6500000).
  Keep the unit the person used (per bag / total / per sq ft) in the unit field.
- An extra charge mentioned separately (transport, GST, loading) is NOT part of the unit price.
- call_outcome: completed | no_answer | wrong_number | callback_requested | refused | cut_off.
- follow_up_needed: true if any must_collect field is tentative, refused or not_mentioned.
Output only valid JSON matching the given schema.
