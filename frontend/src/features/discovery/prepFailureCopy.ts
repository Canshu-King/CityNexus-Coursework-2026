/**
 * ORD-0440 second half - the preparation panel's failure sentences come from here, not from the answer.
 *
 * The panel used to print `error.message` / `job.error.message` straight into the DOM, which is what
 * the exposure ruler counts as `UPSTREAM_EXCEPTION_TO_DOM`: an upstream sentence reaching a traveller,
 * in whatever language the server happened to answer in. The ruling's shape is that a sentence is
 * local and a fact is the server's, so:
 *
 *   - `nameTheFailure` reads only the machine tokens the contract owns (`error.code`,
 *     `error.reason_code`, `error.message_key`) off the parsed envelope that
 *     `lib/rejectionFacts.RejectionWithFacts` already carries, and drops the prose with it;
 *   - `failureSentence` returns a literal from this file, chosen by that token;
 *   - nothing here is ever mounted from an upstream string, and no upstream string rides into a
 *     `data-*` either - `error_receipt_id` and friends are not among the six fields a slot may name
 *     (`components/CnxMeasuredFacts`), so the code stays in the component and decides which sentence
 *     the traveller reads.
 *
 * A code this file does not know gets the kind's own fallback sentence, which is a real reading
 * ("this build has no words for that refusal yet") rather than a laundered one.
 */

export type FailureKind = "read" | "check" | "start" | "stop";

export type NamedFailure = { code: string | null; keys: string[] };

const ARMS: Record<"zh" | "en", Record<string, string>> = {
  zh: {
    "read": "这一次没读到准备的进度。行程没有因此变化，可以再问一次。",
    "check": "这一次没读到这座城市的判定。行程没有因此变化，可以再问一次。",
    "start": "没能开始准备。这一趟行程没有被改动。",
    "stop": "没能停止这次准备。它还在服务器上跑着。",
    "DESTINATION_RESOLVER_UNAVAILABLE": "目的地来源这一次没答上。这不是这座城不能规划，可以再问一次。",
    "DESTINATION_IDENTITY_UNRESOLVED": "这座城还没有能被追溯的来源命名，所以没有可准备的东西。",
    "DESTINATION_AMBIGUOUS": "这个名字对应不止一处，服务器没有替我挑一个。请选一个再说。",
    "CATALOG_STORE_BUSY": "目录这一会儿腾不出手，稍后再试一次。",
    "NETWORK": "这台设备没连上服务器。屏上的判定是上一次读到的，不是这一次的。",
    "HTTP": "服务器这一答不是成功答。屏上没有改成别的结论。",
  },
  en: {
    "read": "This read of the preparation's progress did not come back. Nothing about the trip changed; asking again is safe.",
    "check": "This read of the destination's verdict did not come back. Nothing about the trip changed; asking again is safe.",
    "start": "The preparation did not start. This trip was not changed.",
    "stop": "The preparation is still running on the server; this stop did not go through.",
    "DESTINATION_RESOLVER_UNAVAILABLE": "The destination source did not answer this time. That does not mean the city cannot be planned - ask again.",
    "DESTINATION_IDENTITY_UNRESOLVED": "No traceable source names this city yet, so there is nothing to prepare for it.",
    "DESTINATION_AMBIGUOUS": "That name matches more than one place and the server did not pick one. Choose, then ask again.",
    "CATALOG_STORE_BUSY": "The catalog is busy for the moment. Try again shortly.",
    "NETWORK": "This device could not reach the server. What is on screen is the last real reading, not this one.",
    "HTTP": "The server's answer was not a success. Nothing on screen was rewritten to look like one.",
  },
};

function envelopeFields(envelope: unknown): Record<string, unknown> {
  if (typeof envelope !== "object" || envelope === null) return {};
  const record = envelope as Record<string, unknown>;
  const inner = record.error ?? record.detail;
  if (typeof inner === "object" && inner !== null) return inner as Record<string, unknown>;
  return record;
}

/** The machine tokens only. The answer's prose is dropped here and never returned. */
export function nameTheFailure(cause: unknown): NamedFailure {
  const envelope =
    cause && typeof cause === "object" && "envelope" in cause
      ? (cause as { envelope?: unknown }).envelope
      : cause;
  const fields = envelopeFields(envelope);
  const keys = ["code", "reason_code", "message_key"]
    .map((field) => fields[field])
    .filter((value): value is string => typeof value === "string" && value.trim() !== "")
    .map((value) => value.trim().toUpperCase());
  const status =
    cause && typeof cause === "object" && "http_status" in cause
      ? (cause as { http_status?: unknown }).http_status
      : undefined;
  const code = keys[0] ?? (typeof status === "number" && status >= 400 ? "HTTP" : null);
  return { code, keys };
}

/**
 * One sentence, from this file. A connectivity refusal is named only when the carrier says the
 * request never got an answer at all - a 200 that refuses is a fact about the destination, and
 * printing 网络不可用 for it is the exact laundering `lib/failureAttribution` was written to stop.
 */
export function failureSentence(kind: FailureKind, named: NamedFailure, zh: boolean): string {
  const arm = zh ? ARMS.zh : ARMS.en;
  const code = named.code && arm[named.code] ? named.code : null;
  return code ? arm[code] : arm[kind];
}

export const FAILURE_SENTENCE_CODES = Object.keys(ARMS.zh).filter(
  (key) => !["read", "check", "start", "stop"].includes(key),
);
