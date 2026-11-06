/**
 * ORD-0274 B2c: a non-idempotent request that is still in flight is a fact the update handover has to
 * respect, not something to walk through.
 *
 * The worker swap and the page reload happen in the document, but the write lives in the document too:
 * reloading while it is outstanding cuts the request in half from the user's point of view -- the server
 * may still finish it while the client has forgotten it ever asked. That is a different defect from the
 * one 0274-B2 fixed (an open document that never converges), so this module exists to let convergence
 * wait for a safe point instead of either (a) never converging or (b) requiring another navigation.
 *
 * Only methods that are not safe to repeat are counted. A GET that gets abandoned is a GET; a POST that
 * gets abandoned might already have written a row, and this app has idempotency work in flight elsewhere
 * (ORD-0253) rather than a guarantee I can lean on here.
 */

const IDEMPOTENT_METHODS = new Set(["GET", "HEAD"]);

let inFlight = 0;
const quietListeners = new Set<() => void>();

/** Bracket one request. `begin` returns the release function exactly once, whatever the outcome. */
export function beginNonIdempotentRequest(method: string | undefined): () => void {
  const verb = (method ?? "GET").toUpperCase();
  if (IDEMPOTENT_METHODS.has(verb)) return () => {};

  inFlight += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    inFlight = Math.max(0, inFlight - 1);
    if (inFlight === 0) {
      for (const listener of [...quietListeners]) listener();
    }
  };
}

export function hasNonIdempotentRequestInFlight(): boolean {
  return inFlight > 0;
}

/** How many are outstanding right now. Telemetry only; the handover decision is a boolean. */
export function nonIdempotentRequestsInFlight(): number {
  return inFlight;
}

/**
 * Run `then` at the next quiet point. With nothing in flight that point is now, so a caller that just
 * wants "as soon as it is safe" never has to special-case an idle page.
 */
export function onNonIdempotentRequestsQuiet(then: () => void): () => void {
  if (inFlight === 0) {
    then();
    return () => {};
  }
  quietListeners.add(then);
  return () => quietListeners.delete(then);
}
