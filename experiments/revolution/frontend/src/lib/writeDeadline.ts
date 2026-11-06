/**
 * ORD-0281 (keyed half) -- a write leg that carries an idempotency key gets its own deadline.
 *
 * The symptom this closes is measured, not assumed: a `plans/generate` that lands inside a SQLite write-lock
 * window waited out the whole 30 s lock wait (`32286 / 32328 / 32335 ms` on two generations) with no
 * client-side bound at all, because `readDeadline.ts` deliberately covers reads and 13 of the write sites never
 * entered that wrapper even by name. "There is a timeout somewhere" and "the request I sent is bounded" are
 * different claims; only the second one is a traveller-facing fact.
 *
 * Why the key decides it (Q33=① cut this order in half on exactly this line): aborting a request we can replay
 * leaves a durable answer to go back and read -- the same key re-sends the same question and either replays or
 * is answered. Aborting a request the server cannot recognise leaves an *unknown* state, and relabelling that as
 * "we gave up" is the 0288 argument, still held for arbitration. So a keyless write is passed through untouched
 * and that pass-through is a named line, not an accident.
 *
 * Two numbers must not fight. The server's own budget for one write is 45 s over a 30 s lock wait
 * (`backend/app/core/config.py:95-96`, `backend/app/db/write_deadline.py`), so a client bound BELOW that would
 * cancel work the server was still legitimately doing and report the server's own success as a failure. This
 * constant therefore sits strictly above the whole server chain, and the test that names it checks the order of
 * the two numbers, not just the number (`writeDeadlineNumbersAreOrdered`).
 *
 * A manual cancel stays a different fact from a deadline: an abort that came from the traveller's own signal is
 * rethrown as it is, never renamed `WriteDeadlineMissed` -- otherwise the deadline impersonates the user, which
 * is the same attribution mistake ORD-0280 just closed for the label.
 */

export const CLIENT_WRITE_DEADLINE_MS = 60_000;
export const CLIENT_WRITE_DEADLINE_SECONDS = CLIENT_WRITE_DEADLINE_MS / 1000;
/** The server's own per-write budget, restated here only so the ordering test has both sides in one file. */
export const SERVER_WRITE_DEADLINE_MS_UNDER_TEST = 45_000;

const IDEMPOTENCY_HEADER = "idempotency-key";

/** Thrown when OUR timer ended a keyed write. Its own name, deliberately not `ReadDeadlineMissed`. */
export class WriteDeadlineMissed extends Error {
  readonly deadlineMs: number;
  readonly method: string;
  readonly path: string;
  constructor(method: string, path: string, deadlineMs: number) {
    super(`the ${method} to ${path} was ended by this device after ${deadlineMs} ms`);
    this.name = "WriteDeadlineMissed";
    this.deadlineMs = deadlineMs;
    this.method = method;
    this.path = path;
  }
}

export function isWriteDeadlineMissed(error: unknown): boolean {
  return error instanceof Error && error.name === "WriteDeadlineMissed";
}

/**
 * Does this request carry a key we could replay with?
 *
 * Read off the request's own headers at call time (`Headers.forEach` lowercases names, so casing in the literal
 * does not matter). A key that arrives some other way -- e.g. set by a service worker -- is NOT looked for: this
 * decides whether *this call site* may be aborted, and only the caller knows that.
 */
export function carriesIdempotencyKey(init: RequestInit): boolean {
  const headers = init.headers;
  if (!headers) return false;
  if (headers instanceof Headers) return headers.has(IDEMPOTENCY_HEADER);
  if (Array.isArray(headers))
    return headers.some(
      (pair) => Array.isArray(pair) && typeof pair[0] === "string" && pair[0].toLowerCase() === IDEMPOTENCY_HEADER,
    );
  return Object.keys(headers).some((k) => k.toLowerCase() === IDEMPOTENCY_HEADER);
}

/**
 * The traveller-facing sentence, from one source so a second inline copy cannot be made (T-c's killer).
 *
 * It names who is speaking -- this device's bound ran out -- and never claims the server said it. The status code
 * of a request that was never answered is not a server statement, and this is the same distinction ORD-0280 made
 * for 「服务器给出的原因」. It also refuses to promise the write did not happen, which is T-d's requirement.
 */
export function writeDeadlineCopy(
  zh: boolean,
  deadlineMs: number = CLIENT_WRITE_DEADLINE_MS,
): { title: string; body: string; seconds: number } {
  const seconds = Math.round(deadlineMs / 1000);
  return zh
    ? {
        title: `这台设备等到 ${seconds} 秒就停了`,
        body:
          "服务端没有在这段时间内回答，所以这一笔的结果是未知的——不是「没有发生」。" +
          "请先刷新核对再决定是否重试；同一把幂等键重试是补问同一笔，不会生成第二份。",
        seconds,
      }
    : {
        title: `This device stopped waiting after ${seconds} s`,
        body:
          "The server did not answer in that time, so the outcome of this write is unknown -- not " +
          '"did not happen". Refresh and check before deciding to retry; retrying with the same ' +
          "idempotency key re-asks the same write instead of creating a second one.",
        seconds,
      };
}

/** The server budget is the thing we must not cut in front of; exported so the gate can assert the order. */
export function writeDeadlineNumbersAreOrdered(): {
  client_ms: number;
  server_ms: number;
  client_above_server: boolean;
} {
  return {
    client_ms: CLIENT_WRITE_DEADLINE_MS,
    server_ms: SERVER_WRITE_DEADLINE_MS_UNDER_TEST,
    client_above_server: CLIENT_WRITE_DEADLINE_MS > SERVER_WRITE_DEADLINE_MS_UNDER_TEST,
  };
}

/**
 * `fetch` for a keyed non-read request, bounded by our own timer.
 *
 * Reads (GET/HEAD) and keyless writes both pass straight through: the first belong to `fetchWithReadDeadline`,
 * the second to ORD-0288, which is still held. Honouring a caller-supplied signal means the traveller's
 * 「取消」 keeps arriving as an abort and never gets relabelled as our timeout.
 */
export async function fetchWithWriteDeadline(
  input: string,
  init: RequestInit = {},
  deadlineMs: number = CLIENT_WRITE_DEADLINE_MS,
): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const isWrite = method !== "GET" && method !== "HEAD";
  if (!isWrite || !carriesIdempotencyKey(init)) {
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
    if (firedHere) throw new WriteDeadlineMissed(method, input, deadlineMs);
    throw error;
  } finally {
    clearTimeout(timer);
    if (upstream) upstream.removeEventListener("abort", onUpstreamAbort);
  }
}
