/**
 * ORD-0269 — journey-scoped continuation detection, Phase 1 (client only).
 *
 * Detection predicate (per arbiter Q12, five clauses): a persisted journey-scoped marker written
 * AFTER leg 1 returned its mission_id and BEFORE leg 2 was ever sent, joined against the real
 * agent-feed events for that exact mission_id. Field names are pinned from a real payload
 * (review/evidence/ord0269-real-envelope-fields.json) — never guessed.
 *
 * Named coverage boundary: the marker is device-local. A different device (or cleared storage)
 * shows nothing — that is the named boundary, not a failure (clause 3).
 *
 * Status discipline: the feed carries agent.run.completed with facts.status, but mission terminal
 * status is NOT a feed field today, so nothing here claims "mission terminal is readable".
 *
 * Pagination (R-6): the feed route defaults to limit=100 and orders ascending, so the newest
 * aborted round can be pushed out of the page. Callers pass limit=200, and this module treats
 * "page does not contain the marker's mission" as UNCONFIRMABLE — a named cell-3 state, never a
 * silent pass and never a timestamp-based guess.
 */

export type ContinuationState =
  | { kind: "none" }
  | { kind: "pending"; missionId: string }
  | { kind: "unconfirmable"; reason: "feed_unreadable" | "mission_out_of_page" };

export type AgentFeedEvent = {
  event_type?: string;
  entity_id?: string | null;
  facts?: Record<string, unknown> | null;
};

export type AgentFeedPayload = { events?: AgentFeedEvent[] } | null;

const MARKER_KEY = (journeyId: string) => `citynexus.v8.continuation.${journeyId}`;

export type ContinuationMarker = { missionId: string; markedAt: string };

/** Leg 1 returned its mission_id: persist the pending marker BEFORE leg 2 can be sent (clause 1). */
export function markPendingContinuation(journeyId: string, missionId: string): void {
  const marker: ContinuationMarker = {
    missionId,
    markedAt: new Date().toISOString(),
  };
  try {
    window.localStorage.setItem(MARKER_KEY(journeyId), JSON.stringify(marker));
  } catch {
    /* storage unavailable: detection then simply has no marker — the named boundary, not a crash */
  }
}

/** The marker leaves only when the continuation produces a visible outcome or is superseded. */
export function clearPendingContinuation(journeyId: string): void {
  try {
    window.localStorage.removeItem(MARKER_KEY(journeyId));
  } catch {
    /* nothing to clear */
  }
}

export function readPendingContinuation(journeyId: string): ContinuationMarker | null {
  try {
    const raw = window.localStorage.getItem(MARKER_KEY(journeyId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ContinuationMarker;
    if (typeof parsed?.missionId !== "string" || parsed.missionId.length === 0) return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * Join the marker against the real feed events (clause 2). The caller passes the payload it
 * already fetched with limit=200; this module never guesses a field name.
 */
export function classifyContinuation(
  journeyId: string,
  feed: AgentFeedPayload,
): ContinuationState {
  const marker = readPendingContinuation(journeyId);
  if (!marker) return { kind: "none" };
  if (!feed || !Array.isArray(feed.events)) {
    return { kind: "unconfirmable", reason: "feed_unreadable" };
  }
  const missionEvents = feed.events.filter(
    (event) =>
      event?.event_type === "agent.mission.created" &&
      (event as { facts?: { mission_id?: unknown } }).facts?.mission_id === marker.missionId,
  );
  if (missionEvents.length === 0) {
    /* The marker's mission is not on this page: with an ascending, capped feed that proves nothing
       in either direction — the named R-6 shape, reported as unconfirmable rather than resolved. */
    return { kind: "unconfirmable", reason: "mission_out_of_page" };
  }
  /* Run observation: quote what the feed carries, without upgrading it to a mission-terminal claim. */
  const runCompleted = feed.events.some(
    (event) =>
      event?.event_type === "agent.run.completed" &&
      (event as { facts?: { mission_id?: unknown } }).facts?.mission_id === marker.missionId,
  );
  return { kind: "pending", missionId: marker.missionId, ...(runCompleted ? { runObserved: true } : {}) } as ContinuationState;
}

/** What a continuation attempt can come back as.  Four named shapes; none of them is "silence". */
export type ContinuationOutcome =
  | { kind: "accepted"; planIds: string[] }
  | { kind: "refused"; httpStatus: number; serverReason: string | null }
  | { kind: "deadline"; waitedMs: number }
  | { kind: "transport"; detail: string };

/**
 * ORD-0278 -- re-issue the SECOND leg for a mission the marker already names.
 *
 * Field names are not guesses: `mission_id` is a declared field of the landed
 * `GeneratePlansRequest` (backend/app/schemas.py, ORD-0272), and `Idempotency-Key` as a header is
 * the replan route's own precedent (backend/app/main.py).  The two keys stay two keys on purpose:
 * the header answers "is this the same send", the body answers "whose batch is this", and merging
 * them makes "same mission, different request" indistinguishable from "same request, resent".
 *
 * The marker is NOT cleared here -- that is the caller's decision and only after the traveller can
 * see an outcome (ORD-0269 clause 5).  `deadlineMs` is supplied by the caller so a resume cannot
 * open a second unbounded wait.
 */
export async function continuePendingPlan(args: {
  journeyId: string;
  missionId: string;
  idempotencyKey: string;
  deadlineMs: number;
  fetchImpl?: typeof fetch;
}): Promise<ContinuationOutcome> {
  const send = args.fetchImpl ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort("deadline"), args.deadlineMs);
  try {
    const response = await send(`/api/v1/journeys/${args.journeyId}/plans/generate`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "Idempotency-Key": args.idempotencyKey,
      },
      // candidate_count matches the sheet's own first run so a resumed round is not a different
      // request shape pretending to be the same intent.
      body: JSON.stringify({ candidate_count: 3, mission_id: args.missionId }),
      signal: controller.signal,
    });
    const payload = (await response.json().catch(() => null)) as unknown;
    if (!response.ok) {
      const detail = payload as { detail?: unknown; error?: { message?: unknown }; reason_code?: unknown } | null;
      const serverReason =
        (typeof detail?.error?.message === "string" ? detail.error.message : null) ??
        (typeof detail?.detail === "string" ? detail.detail : null) ??
        null;
      return { kind: "refused", httpStatus: response.status, serverReason };
    }
    const planIds = (Array.isArray(payload) ? payload : [])
      .map((item: { id?: unknown }) => (typeof item?.id === "string" ? item.id : ""))
      .filter((id: string) => id.length > 0);
    return { kind: "accepted", planIds };
  } catch (cause) {
    if (cause instanceof Error && cause.name === "AbortError") {
      return { kind: "deadline", waitedMs: args.deadlineMs };
    }
    return { kind: "transport", detail: cause instanceof Error ? cause.name : "Unknown" };
  } finally {
    clearTimeout(timer);
  }
}

