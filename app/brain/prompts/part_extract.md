You read a plumber's request to find a part (Telugu / English / Hindi / mixed; digits already normalised) and,
optionally, a photo reading of the part or its label.
Return:
- description: short part name ("angle valve", "health faucet", "flush tank").
- brand, size ("1/2 inch", "15 mm") if given.
- model: the model code EXACTLY as written or as read from the label (keep every character; never correct, never invent).
  If the model is not given and not readable, model = null and model_unreadable = true.
- qty: number of pieces.
- shops: the shop names the plumber wants called, as he said them (e.g. ["Srinivas", "Ravi"]).
- dependency: only if he states a rule like "if Srinivas has the exact one under 500, ask the customer for 4 to 6":
  {shop, exact_model (true unless he says any model is fine), max_unit_price, then_label ("today 4–6 pm")}; else null.
Output only valid JSON.
