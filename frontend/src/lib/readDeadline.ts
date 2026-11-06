/**
 * ORD-0263 -- every data *read* carries a deadline, so "the origin never answered" becomes an event
 * the app can classify instead of a traveller left standing on a loading skeleton.
 *
 * Before this, `api/citynexus.ts` had no `AbortController` and no timeout anywhere in its request
 * path (measured, not guessed): against an origin that accepts the TCP connection and writes nothing
 * back, the `await` never settled, the `catch` never ran, `busy` stayed true, and the three states
 * the screen is supposed to tell apart were down to two -- because the third one never happened.
 *
 * Reads only, deliberately -- until ORD-0281.  Aborting a *write* at a deadline tells the client the request is
 * over while the server may still be finishing it, which is an idempotency question this module does not get to
 * answer by silence.  ORD-0281 answered it for the one case where a replay is well-defined: a write carrying an
 * `Idempotency-Key`, handed to `fetchWithWriteDeadline` below.  Keyless writes still pass straight through,
 * because "what does an aborted write the server cannot recognise mean?" is ORD-0288 and is still held for
 * arbitration -- so the pass-through is a named decision with an owner, not the old silence.
 */

import { fetchWithWriteDeadline } from "./writeDeadline";

export const READ_DEADLINE_MS = 12_000;
export const READ_DEADLINE_SECONDS = READ_DEADLINE_MS / 1000;

/** Thrown when our own deadline fired -- distinct from a transport error and from an HTTP answer. */
export class ReadDeadlineMissed extends Error {
  readonly deadlineMs: number;
  constructor(deadlineMs: number) {
    super(`no answer within ${deadlineMs} ms`);
    this.name = "ReadDeadlineMissed";
    this.deadlineMs = deadlineMs;
  }
}

export function isReadDeadlineMissed(error: unknown): boolean {
  return error instanceof Error && error.name === "ReadDeadlineMissed";
}

/**
 * `fetch` with a per-read deadline.  A caller-supplied `signal` is honoured, not overwritten: an
 * abort that came from the traveller ("stop waiting") must stay a different fact from an abort that
 * came from this timer, or the deadline would impersonate the user.
 */
export async function fetchWithReadDeadline(
  input: string,
  init: RequestInit = {},
  deadlineMs: number = READ_DEADLINE_MS,
): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    /* ORD-0281: the keyed subset is bounded inside this call; a keyless write is still passed through
       (ORD-0288, held).  One decision point, so a leg cannot be "server's answer" here and "unknown" there. */
    return fetchWithWriteDeadline(input, init);
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
    if (firedHere) throw new ReadDeadlineMissed(deadlineMs);
    throw error;
  } finally {
    clearTimeout(timer);
    if (upstream) upstream.removeEventListener("abort", onUpstreamAbort);
  }
}
