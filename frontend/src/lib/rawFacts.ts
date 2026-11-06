/**
 * What this device measured on a response, before it tried to understand any of it.
 *
 * Q64=1 (arbiter): an upstream sentence may not reach the DOM, so the technical facts a screen may
 * still show are *this device's own readings* -- the status it saw, the media type the answer
 * carried, how many octets it was.  Field names are labels; the values are the answer's own,
 * unedited.  This is the shape `PlanWithAiSheet.tsx` was accepted with under ORD-0302 b5, lifted out
 * of that one sheet so the three ORD-0325 fixes do not each re-implement it.
 */

import { readErrorReceipt, type ReceiptView } from "./errorReceipt";

export type RawFacts = {
  http_status: number | null;
  content_type: string | null;
  bytes: number | null;
  /**
   * ORD-0353: the recitable number, read out of the same text the byte count measured.  Carried
   * here rather than at each mount so a screen cannot print a 编号 the server did not send --
   * `readErrorReceipt` returns the named missing token when the key is absent, empty or malformed.
   */
  receipt: ReceiptView;
};

export function measureRawFacts(
  response: { status: number; headers: { get(name: string): string | null } },
  rawText: string,
): RawFacts {
  return {
    http_status: response.status,
    content_type: response.headers.get("content-type"),
    bytes: new TextEncoder().encode(rawText).length,
    receipt: readErrorReceipt(parseJsonOrNull(rawText)),
  };
}

/**
 * Read an answered response exactly once and keep both halves: the text, so a machine token can be
 * read out of it, and the measurement, so the screen can show what it never received.
 */
export async function readResponseFacts(
  response: {
    status: number;
    headers: { get(name: string): string | null };
    text(): Promise<string>;
  },
): Promise<{ text: string; facts: RawFacts }> {
  const text = await response.text();
  return { text, facts: measureRawFacts(response, text) };
}

/**
 * `response.json().catch(() => null)` was the only way these screens looked at a refusal, and that
 * one line collapsed zero bytes, an HTML document and a plain sentence into the same `null`
 * (ORD-0263).  Parsing by hand from the same text the byte count measured keeps the two apart.
 */
export function parseJsonOrNull(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * The three whitelisted readings, each carrying the Q64 contract name it stands for.
 *
 * ORD-0353 ③ / Q70: the words printed beside a number belong to this screen, so the machine-readable
 * name has to travel on the element holding the value.  `field` is that name in the contract's own
 * spelling (`body_bytes`), and `line` is what the mount prints -- the two are deliberately not the
 * same string, which is the whole point of the anchor.
 */
export function rawFactEntries(
  facts: RawFacts,
): Array<{ field: string; value: string; line: string }> {
  const readings: Array<{ field: string; label: string; value: number | string | null }> = [
    { field: "http_status", label: "http_status", value: facts.http_status },
    { field: "content_type", label: "content-type", value: facts.content_type },
    { field: "body_bytes", label: "bytes", value: facts.bytes },
  ];
  return readings.map((reading) => {
    const value = reading.value == null ? "" : String(reading.value);
    return { field: reading.field, value, line: `${reading.label}=${value}` };
  });
}

/**
 * The condition a refusal names for itself, matched against a list this screen already knows.
 *
 * Some raise sites put the name in the envelope (`error.code`); some prefix it inside the human
 * sentence (`detail: "STAY_ITINERARY_CONFLICT: ..."`).  Both cases are a token, so the token is what
 * is compared and the sentence is what is dropped -- scanning the answer's text rather than one
 * assumed path is what keeps a re-shaped envelope from silently turning every conflict back into a
 * generic refusal.
 */
export function namedCodeIn(text: string, known: readonly string[]): string | null {
  for (const token of text.match(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g) ?? []) {
    if (known.includes(token)) return token;
  }
  return null;
}
