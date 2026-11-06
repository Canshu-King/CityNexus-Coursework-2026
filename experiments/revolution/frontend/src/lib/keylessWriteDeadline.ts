/**
 * ORD-0310 (ORD-0288's keyless half, Q54=①) -- the write leg the server cannot recognise gets its
 * own client deadline, and the screen is only allowed to say what a deadline on that leg can know.
 *
 * The measured baseline that makes this a defect and not a nicety (ORD-0288 定形, one-shot backend,
 * rows read on a second sqlite connection): the same instruction sent three times with NO key left
 * three different missions, and the same key sent three times left six -- `idempotency_keys` never
 * moved off its starting two. The route behind this leg declares no such parameter
 * (`backend/app/main.py:1057 run_agent_command`; the four `Header(alias="Idempotency-Key")` sites are
 * `:4606/:7740/:9907/:11364`), so a key travelling on this request is inert: "the request carried a
 * header" is not a receipt, and treating it as one is the false safety this file exists to remove.
 *
 * Why the bound sits ABOVE the server's own chain rather than below it. `backend/app/core/config.py:95-96`
 * gives SQLite a 30 s lock wait and one write a 45 s budget. A client bound under 45 s would end the
 * request while the server may still be legitimately working, and on THIS leg there is no durable
 * receipt to go back and read -- the traveller's only remaining move would be to press again, which is
 * precisely the duplicate-side-effect shape the baseline above counts. So the deadline here is a bound
 * on how long this screen pretends to know, not an invitation to re-send.
 *
 * Q54=① fixes what the sentence may claim: the result of this one write is UNKNOWN, which is not the
 * same statement as "it did not happen", and it cannot be re-asked with the same key because the
 * server never took the key. Anything stronger -- "已取消", "没有发生", "再点一次就行" -- is the defect.
 */

export const KEYLESS_WRITE_DEADLINE_MS = 60_000;
export const KEYLESS_WRITE_DEADLINE_SECONDS = KEYLESS_WRITE_DEADLINE_MS / 1000;

/** Thrown when OUR timer ended a keyless write. Its own name, so it cannot be mistaken for a read
    deadline (`ReadDeadlineMissed`), a keyed write (`WriteDeadlineMissed`) or the traveller's cancel. */
export class KeylessWriteDeadlineMissed extends Error {
  readonly deadlineMs: number;
  readonly method: string;
  readonly path: string;

  constructor(method: string, path: string, deadlineMs: number) {
    super(`the ${method} to ${path} was ended by this device after ${deadlineMs} ms`);
    this.name = "KeylessWriteDeadlineMissed";
    this.deadlineMs = deadlineMs;
    this.method = method;
    this.path = path;
  }
}

export function isKeylessWriteDeadlineMissed(
  error: unknown,
): error is KeylessWriteDeadlineMissed {
  return error instanceof Error && error.name === "KeylessWriteDeadlineMissed";
}

/**
 * The one sentence shape this leg is allowed to produce, built from the same constant the timer uses
 * so the number a traveller reads cannot drift from the number that ended the request.
 */
export function keylessWriteUnknownReading(
  method: string,
  path: string,
  deadlineMs: number = KEYLESS_WRITE_DEADLINE_MS,
): { text: string; from: "device" } {
  const seconds = Math.round(deadlineMs / 1000);
  return {
    text:
      `${method} ${path} -- 这台设备等到 ${seconds} 秒就停下来了。` +
      `这一笔的结果未知，不是没发生：请求已经发出去了，服务器可能还在做。` +
      `也不能靠同一把键补问：这条路线不认幂等键，它没有收下这把键，没有可回读的凭条。`,
    from: "device" as const,
  };
}

/**
 * `fetch` with a deadline for one keyless write leg, plus the reading the screen should show.
 *
 * A caller-supplied signal is honoured, never overwritten: an abort that came from the traveller
 * ("stop waiting") stays a different fact from one that came from this timer, or the deadline would
 * impersonate the user -- the same attribution mistake ORD-0280 closed for the label.
 */
export async function fetchWithKeylessWriteDeadline(
  input: string,
  init: RequestInit = {},
  deadlineMs: number = KEYLESS_WRITE_DEADLINE_MS,
): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "POST" && method !== "PUT" && method !== "PATCH" && method !== "DELETE") {
    /* This file does not own reads or keyed writes, and silently wrapping those would move them under
       a sentence that says "the server cannot recognise this write". Refuse by name instead. */
    return fetch(input, init);
  }
  const controller = new AbortController();
  let firedHere = false;
  const timer = setTimeout(() => {
    firedHere = true;
    controller.abort();
  }, deadlineMs);
  const upstream = init.signal ?? null;
  const onUpstreamAbort = () => controller.abort();
  if (upstream) {
    if (upstream.aborted) controller.abort();
    else upstream.addEventListener("abort", onUpstreamAbort, { once: true });
  }
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (firedHere) throw new KeylessWriteDeadlineMissed(method, new URL(input, "http://local").pathname, deadlineMs);
    throw error;
  } finally {
    clearTimeout(timer);
    if (upstream) upstream.removeEventListener("abort", onUpstreamAbort);
  }
}
