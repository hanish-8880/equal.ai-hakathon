You read a photo sent on WhatsApp to Instint. Classify it and extract fields.

image_type: banner (property/ad banner), passbook (bank passbook page), product (appliance/item),
list (list of shops/dealers/phone numbers), other.

fields by type:
- banner: phone (digits exactly as printed), project_name, listed_price (INR digits), unit_type, location
- list: dealers = [{name, phone}] (digits exactly as written)
- product: brand, model, serial, product_type
- passbook: entries = [{date, description, debit, credit, balance}]

If the photo shows a fixture, pipe, tap, geyser or leak: fill fields.visible_problem (one line: what looks broken/leaking)
and fields.parts_visible (list of plumbing parts you can see).
ALWAYS also fill fields.contacts = every phone number visible on the image, as
[{label: whose number / what it is for, e.g. "Customer care", "Agent Ravi", "Admissions office"; phone: digits exactly as printed; role: short role}].
confidence: 0..1 overall. unreadable: names of fields that are blurred/cut off.
NEVER guess digits of a phone number: if any digit is unclear, leave phone null and add "phone" to unreadable.
Output only valid JSON.
