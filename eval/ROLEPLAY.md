# Role-play kit — real calls for the eval

Each volunteer agrees to receive Chinku calls on their own phone (consent first). They play a shop owner and
say what their card says, in their own words, in any language. Because the card fixes the answers, the
expected labels are known **before** the call; audio, accent, network and speech recognition are real.

How to run one card:
1. Hanish sends Chinku (WhatsApp or web chat): `<item> kavali. <Name> <volunteer number> ki call chesi best rate kanukko`
   then `yes`.
2. Volunteer answers the call using the card. Natural speech, own words, any language, interruptions are fine.
3. After the call: `python -m eval.collect --since "<time IST>"` pulls the transcript into `eval/calls/` with the card's labels.

Mark 5 cards as **fresh** (never looked at while changing prompts).

| Card | Item | What the volunteer says | Expected (value · status) |
|---|---|---|---|
| A1 | 20 bags cement | "390 per bag. Delivery day after tomorrow, sure. Transport 500 extra. Cash on delivery." | price 390·confirmed, delivery day after tomorrow·confirmed, transport 500·confirmed, payment cash on delivery·confirmed |
| A2 | 20 bags cement | "Somewhere 380 to 395, depends on brand… I'll call back with exact. Delivery maybe Thursday." (won't say transport/payment) | price 380–395·tentative, delivery Thursday·tentative, transport ·not_mentioned, payment ·not_mentioned |
| A3 | 10 chairs | "1,200 each… no wait, for 10 I'll give 1,100. Delivery tomorrow. Transport free. Full advance." | price 1100·confirmed (changed answer!), delivery tomorrow·confirmed, transport 0·confirmed, payment full advance·confirmed |
| A4 | 2 ton steel | "Rates on phone we don't give. Come to shop." Then: "Delivery within 3 days we can do." | price ·refused, delivery within 3 days·confirmed, transport ·not_mentioned, payment ·not_mentioned |
| A5 | 50 bags cement | Speak Telugu numbers: "Bag ki moodu vandala enabhai… transport rendu velu… delivery repu pakka… payment half advance" | price 380·confirmed, transport 2000·confirmed, delivery tomorrow·confirmed, payment half advance·confirmed |
| A6 | 50 bags cement | "Price 400, including transport. Delivery Monday, but if rain, Tuesday. Payment 30 days credit." | price 400·confirmed, transport 0 (included)·confirmed, delivery Monday·tentative, payment 30 days credit·confirmed |
| A7 | 5 fans | "1,800 each… actually that's last month's rate, I'll check and confirm." Then hang up. | price 1800·tentative, others ·not_mentioned |
| A8 | 50 bags cement | Answer only the price ("385"), then say "I'm busy, call later" and hang up. | price 385·confirmed, others ·not_mentioned |

Need: 3 volunteers × 5 calls ≈ 15 real cases (≈ 15 min each). Cards can repeat across volunteers (different voices = different cases).
