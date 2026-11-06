# ERROR_RECEIPT_ID_V1 — the one field that carries a rejection's recitable number (ORD-0332)

Produced by lane F `8ccd71d8` on generation `c740f45d68f337c460a1a43b995162689d0110af`.
Implementation: `backend/app/error_receipt.py`, armed by `backend/app/main.py` middleware
`error_receipt_per_request`, folded in at the single envelope builder `backend/app/main.py:error()`.

## The field is single and fixed

| | |
| --- | --- |
| Name | `error_receipt_id` — the only key allowed to carry a receipt. Nothing else may hold one, and no second name is introduced. |
| Where | `error.error_receipt_id`, i.e. inside the existing error envelope next to `code` / `message` / `message_key` / `trace_id` / `details`. Declared in `contracts/openapi.yaml` under `components.schemas.ErrorEnvelope`. |
| Shape | `CNX-` + exactly 8 characters from `23456789ABCDEFGHJKMNPQRSTVWXYZ` (Crockford-style: no `I` `L` `O` `U` `0` `1`). |
| Regex | `^CNX-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$` |

The alphabet is the point, not the length: 36-char UUIDs (what `trace_id` is) cannot be read back over
the phone without ambiguity, which is why `trace_id` exists and still does not satisfy ORD-0332.

## How it is derived, and why a retry gives the same answer

```
key  = method "\0" path "\0" canonical_query "\0" (idempotency-key or "") "\0" code
seed = SHA-256(key)
id   = "CNX-" + join(ALPHABET[b % 32] for b in seed[0:8])
```

* `canonical_query` is `urlencode(sorted(query_params.multi_items()))` — a client that reorders its
  query string is still the same call; a repeated parameter stays distinguishable.
* `code` is inside the digest on purpose: the same call failing with two different reasons is two
  different failures, and one number for both would blur them.
* **Nothing time-, process- or worker-dependent enters the seed.** No clock, no PID, no random draw.
  That is what makes 同一请求重发要相等 a property of the recipe rather than a hope.
* The request **body is not hashed**. A POST that carries an `Idempotency-Key` is scoped by that key
  (the agent domain already treats it as the caller's idempotency scope — `backend/app/agent/gateway.py`
  的原文注释); one that does not is scoped by route + code. This is a declared boundary, not an
  oversight: hashing bodies would make a retried call differ whenever a client re-serialises a field.

`trace_id` keeps its old meaning and stays per-envelope. One envelope may carry both; they are never
the same field and never the same value.

## Absence is an answer

With no request context — a background job, a script, a unit call straight into `error()` — the key is
**not written**. An empty string would be a number-shaped lie, and a screen that reads `""` as
"there is a number, it is blank" is exactly the invention ORD-0332 exists to stop.

## Where to look a receipt up

The receipt's other half is one log line, emitted by the same builder:

```
error_receipt_id=CNX-XXXXXXXX code=<CODE> status=<n> route=<METHOD> <path> trace_id=<uuid>
```

* 5xx → `logger.warning` (the class a screen calls 结果未知; greppable with no reconfiguration).
* 4xx → `logger.info` (so a routine validation refusal cannot flood the log).
* `details` is deliberately not dumped, and the number is **not** written to the database: a failed
  write usually rolls back, and a receipt stored in that transaction would disappear with it. Where a
  path writes an audit row anyway, that row's `trace_id` is set to the same value the envelope carries.

So: read the number aloud → grep it in the log → the line names the route, the class and the envelope.

## What a screen may do with it

* The word 编号 on screen is gated by the shape check above. Fail the shape (missing, empty,
  whitespace, a `code`, or truncated uppercase prose) → render `NOT-EMITTED` and do not use the word.
  This is Q68 in mechanical form.
* A slot labelled 原因编号 keeps carrying a real `code` only — never this field, never a sentence
  (Q68/Q70; the existing slot belongs to lane B).
* The prose that shows the receipt carries `data-cnx-field="error_receipt_id"` (Q70), and no exception
  text goes into any `data-*` / `aria-*` value (Q71). The device-side exit is
  `frontend/src/lib/errorReceipt.ts`; it is pure, adds no second attribution channel, and touches
  neither `frontend/src/lib/failureAttribution.ts` nor `frontend/src/lib/serverReason.ts`.
* `:8000` remains NOT AVAILABLE by decision, so no claim here depends on a live backend: the backend
  half is proven in-process through the ASGI stack and the frontend half by the compiled module.
