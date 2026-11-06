import type { components } from "../../generated/api";
import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";

/**
 * ORD-0402 slice 2 -- the two doors the recycle-bin screen uses, in one file, so the screen itself
 * renders and cannot hide what it asked the server for.
 *
 * BOTH DOORS ARE ON THE LINE NOW. The write door landed with ORD-0399:
 * `POST /api/v1/journeys/{journey_id}/undelete` (`backend/app/main.py:2999`), no `Idempotency-Key` --
 * the durable column decides whether the write happens -- answering `409 JOURNEY_NOT_DELETED` rather
 * than reporting a success that did not occur (`backend/app/main.py:3033`). The read door landed
 * with ORD-0402 slice 1 at `backend/app/main.py:3058`: `GET /api/v1/recycle-bin/journeys`, declared in
 * `contracts/openapi.yaml:487` and generated at `frontend/src/generated/api.ts:414`, each row
 * carrying the stored `deleted_at` and `bin_state` = `IN_RECYCLE_BIN`
 * (`backend/app/schemas.py:263` and `:264`) -- so this screen never has to conclude "it was deleted"
 * from the trip being missing from `GET /api/v1/journeys`, which keeps its own `deleted_at IS NULL`
 * promise (`backend/app/main.py:2951`).
 *
 * The name this file chose is the name slice 1 shipped, so the constant below needed no edit when the
 * door arrived; that is the point of keeping the URL in one place. The reason for not using
 * `/journeys/discarded` still stands and was measured, not guessed (see
 * `review/ord0402/stack_receipt_r1.json`, key `shadowed_collection_name`): the landed
 * `GET /api/v1/journeys/{journey_id}` door is declared earlier in `app.main`, so a one-segment
 * collection name under `/journeys/` is swallowed by it and answers 422 with
 * `loc: ["path","journey_id"]`, `type: "uuid_parsing"`, `message_key: "validation.not_an_id"` (the
 * server's own receipt for that answer was `CNX-TWXNJHE2`).
 *
 * One thing the screen inherits rather than decides: slice 1 orders the list newest-deletion first
 * (`backend/app/main.py:3081`), and the panel renders the array as answered, so the order a traveller
 * sees is the server's order, not a client-side re-sort.
 */
export const TRASH_LIST_PATH = "/recycle-bin/journeys";

/** A binned trip, typed from the contract rather than re-declared here: slice 1 shipped
 *  `RecycleBinJourney` into the generated client (`frontend/src/generated/api.ts:2771`), so the
 *  screen's rows and the server's schema cannot drift apart silently. The guards in the panel stay
 *  anyway -- a model says what the server intends to send, not what this response did send. */
export type DiscardedJourney = components["schemas"]["RecycleBinJourney"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) throw await rejectionWithFacts(response);
  return (await response.json()) as T;
}

export function readDiscardedJourneys(): Promise<DiscardedJourney[]> {
  return request<DiscardedJourney[]>(TRASH_LIST_PATH);
}

/** The 找回 write. Returns the trip as the server now holds it (`response_model=JourneySchema`). */
export function undeleteJourney(journeyId: string): Promise<{ id: string; title: string }> {
  return request<{ id: string; title: string }>(
    `/journeys/${encodeURIComponent(journeyId)}/undelete`,
    { method: "POST" },
  );
}

/**
 * Which condition refused, in the server's own token, read out of the parsed envelope.
 *
 * `error.reason_code` is where ORD-0101 put it (`backend/app/main.py:598` -- `http_error` forwards
 * `getattr(exc, "reason_code", null)`), and the two names this door can answer with are
 * `JOURNEY_NOT_DELETED` (409) and `JOURNEY_NOT_FOUND` (404). Returning `null` is not a failure: it
 * means this device did not read a name, and the screen then says so instead of picking a sentence.
 */
export function refusalReasonCode(cause: unknown): string | null {
  if (!(cause instanceof RejectionWithFacts)) return null;
  const envelope = cause.envelope;
  if (typeof envelope !== "object" || envelope === null) return null;
  const error = (envelope as Record<string, unknown>).error;
  if (typeof error === "object" && error !== null) {
    const code = (error as Record<string, unknown>).reason_code;
    if (typeof code === "string" && code.trim()) return code;
  }
  const top = (envelope as Record<string, unknown>).reason_code;
  return typeof top === "string" && top.trim() ? top : null;
}

export { RejectionWithFacts };
