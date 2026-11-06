# Multimodal Journey Inbox V2

Supported inputs are structured JSON, ICS, pasted text, images/screenshots, QR/barcodes, and future PDF pages.

- ICS uses `icalendar`.
- QR/barcode decoding uses `@zxing/browser`; deterministic decoding precedes model extraction.
- DeepSeek V4.1 Flash handles ambiguous booking text, reservation screenshots, tickets, notices, and signage.

Vision/text extraction returns strict structured fields with value, confidence, source region, source kind, and `requires_confirmation`. Critical fields may be `UNKNOWN`; the model may not invent values. High-impact reservation facts become Booking/Commitment only after provider validation or explicit user confirmation, then pass Agent Authority and C4 policy.

