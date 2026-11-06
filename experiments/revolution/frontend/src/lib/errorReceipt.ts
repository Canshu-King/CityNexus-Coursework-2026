/**
 * ORD-0332: the device-side exit for a rejection's recitable number.
 *
 * The server owns this value (`contracts/ERROR_RECEIPT_ID_V1.md` fixes the name and the shape). All
 * this module decides is whether the envelope actually carried one, so the word 编号 on a screen can
 * never be invented by the screen: Q68 says a slot may only hold what it names, and a traveller who
 * reads a number aloud must be reading one the server produced.
 *
 * Two rules make that checkable rather than aspirational:
 *  - the value must match the contract shape, so a `code`, a truncated uppercase sentence, an empty
 *    string or whitespace all collapse to `NOT-EMITTED` instead of becoming "a number";
 *  - nothing but the contract field name and a presence flag goes into `attrs`, so no exception text
 *    can ride into `data-*` / `aria-*` (Q71) through this door.
 *
 * It is deliberately not a second attribution channel: it classifies nothing about the failure, and
 * `lib/failureAttribution.ts` is neither imported nor modified here.
 */

export const RECEIPT_FIELD = "error_receipt_id" as const;
/** Crockford-style body: no I/L/O/U/0/1, so the same reading cannot produce two spellings. */
export const RECEIPT_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ";
export const RECEIPT_SHAPE = /^CNX-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$/;
export const RECEIPT_MISSING = "NOT-EMITTED" as const;

export interface ReceiptView {
  /** True only when the server sent a value that matches the contract shape. */
  present: boolean;
  /** The receipt itself, or the literal token that says there is none. Never a made-up number. */
  text: string;
  /** What a mount binds: `data-cnx-field` names the contract field (Q70), nothing else. */
  attrs: Record<string, string>;
  /** What the value actually was, for diagnostics that stay out of the DOM. */
  observed: "present" | "absent-key" | "empty" | "wrong-shape" | "not-an-object";
}

function envelopeOf(input: unknown): Record<string, unknown> | null {
  if (typeof input !== "object" || input === null) return null;
  const record = input as Record<string, unknown>;
  const inner = record.error;
  if (typeof inner === "object" && inner !== null) return inner as Record<string, unknown>;
  return record;
}

/** Reads the receipt out of an error envelope, a parsed body, or a raw fetch payload. */
export function readErrorReceipt(input: unknown): ReceiptView {
  const envelope = envelopeOf(input);
  const attrs = { "data-cnx-field": RECEIPT_FIELD } as Record<string, string>;
  if (envelope === null) {
    attrs["data-cnx-receipt"] = RECEIPT_MISSING;
    return { present: false, text: RECEIPT_MISSING, attrs, observed: "not-an-object" };
  }
  const value = envelope[RECEIPT_FIELD];
  if (typeof value === "string" && RECEIPT_SHAPE.test(value)) {
    attrs["data-cnx-receipt"] = value;
    attrs["data-cnx-receipt-present"] = "true";
    return { present: true, text: value, attrs, observed: "present" };
  }
  const observed: ReceiptView["observed"] =
    value === undefined ? "absent-key" : typeof value === "string" && value.trim() === "" ? "empty" : "wrong-shape";
  attrs["data-cnx-receipt"] = RECEIPT_MISSING;
  attrs["data-cnx-receipt-present"] = "false";
  return { present: false, text: RECEIPT_MISSING, attrs, observed };
}

/**
 * The label a mount may print next to the value. Kept here so a component cannot quietly widen it:
 * it names the server as the source only when the server really was.
 */
export function receiptLabel(present: boolean, zh: boolean): string {
  return present
    ? zh ? "服务器回执号" : "server receipt"
    : zh ? "服务器未给回执号" : "no server receipt";
}
