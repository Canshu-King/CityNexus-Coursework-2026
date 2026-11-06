import type { components } from "../../generated/api";
import {
  refusalReasonCode,
  rejectionWithFacts,
  RejectionWithFacts,
} from "../../lib/rejectionFacts";

/**
 * ORD-0402 slice 2 -- the two doors the recycle-bin screen uses, in one file, so the screen itself
 * renders and cannot hide what it asked the server for.
 *
 * BOTH DOORS ARE ON THE LINE NOW. The write door landed with ORD-0399:
 * `POST /api/v1/journeys/{journey_id}/undelete` (`undelete_journey` in `backend/app/main.py`), no
 * `Idempotency-Key` -- the durable column decides whether the write happens -- answering `409` with
 * `reason_code` = `JOURNEY_NOT_DELETED` rather than reporting a success that did not occur. The read
 * door landed with ORD-0402 slice 1 as `list_recycle_bin_journeys`: `GET /api/v1/recycle-bin/journeys`,
 * declared under that path in `contracts/openapi.yaml` and generated into
 * `frontend/src/generated/api.ts`, each row carrying the stored `deleted_at` and
 * `bin_state` = `IN_RECYCLE_BIN` (`RecycleBinJourney` in `backend/app/schemas.py`) -- so this screen
 * never has to conclude "it was deleted" from the trip being missing from `GET /api/v1/journeys`
 * (`list_journeys`, whose own predicate keeps `deleted_at IS NULL`).
 *
 * Pointers in this header name symbols, not rows. The numeric `file:line` form is what this header
 * used to carry, and it costs a re-measure every time another lane lands above it -- these anchors
 * were re-numbered once already, by the ORD-0535 citation pass. The arm
 * `test_b8_the_screen_owns_no_stale_pointer` in `backend/tests/test_qoder_ord0531_bin_ownership.py`
 * refuses that form in this family.
 *
 * The name this file chose is the name slice 1 shipped, so the constant below needed no edit when the
 * door arrived; that is the point of keeping the URL in one place. The reason for not using
 * `/journeys/discarded` still stands and was measured, not guessed: the landed
 * `GET /api/v1/journeys/{journey_id}` door is declared earlier in `app.main`, so a one-segment
 * collection name under `/journeys/` is swallowed by it and answers 422 with
 * `loc: ["path","journey_id"]`, `type: "uuid_parsing"`, `message_key: "validation.not_an_id"` (the
 * server's own receipt for that answer was `CNX-TWXNJHE2`).
 *
 * One thing the screen inherits rather than decides: slice 1 orders the list newest-deletion first
 * (`list_recycle_bin_journeys` sorts on the stored deletion moment), and the panel renders the array
 * as answered, so the order a traveller sees is the server's order, not a client-side re-sort.
 */
export const TRASH_LIST_PATH = "/recycle-bin/journeys";

/**
 * ORD-0531 -- the same bin, asked from one seat.
 *
 * The path above answers the whole bin to anybody and is kept exactly as it is; this one is the door
 * this screen reads now, because a bin that cannot say whose trip it is leaves the screen to decide
 * whether 找回 belongs there by comparing ids, and that is the guess the order forbids.
 */
export const trashScopePath = (travelerId: string): string =>
  `/travelers/${encodeURIComponent(travelerId)}/recycle-bin/journeys`;

/** The two named shapes the scoped door answers with. Read as strings and compared, never derived. */
export type BinOwnership =
  | "OWNED_BY_THIS_TRAVELER"
  | "OWNED_BY_ANOTHER_TRAVELER"
  | "NOT_STATED";

export type BinRecovery =
  | "RECOVERY_ALLOWED"
  | "RECOVERY_REFUSED_NOT_OWNER"
  | "RECOVERY_REFUSED_ROLE_CANNOT_RESTORE"
  | "RECOVERY_REFUSED_NO_SEAT";

export type ScopedBinRow = DiscardedJourney & {
  ownership?: unknown;
  member_role?: unknown;
  recovery?: unknown;
};

/**
 * What the server said about ownership, or `null` when the row carried no such name.
 *
 * `null` is not "nobody's" and not "mine": the panel renders those rows without an action, because a
 * missing answer is a reason to do nothing, not a reason to guess.
 */
export function binOwnership(row: ScopedBinRow): BinOwnership | null {
  const value = row.ownership;
  return value === "OWNED_BY_THIS_TRAVELER" ||
    value === "OWNED_BY_ANOTHER_TRAVELER" ||
    value === "NOT_STATED"
    ? value
    : null;
}

export function binRecovery(row: ScopedBinRow): BinRecovery | null {
  const value = row.recovery;
  return value === "RECOVERY_ALLOWED" ||
    value === "RECOVERY_REFUSED_NOT_OWNER" ||
    value === "RECOVERY_REFUSED_ROLE_CANNOT_RESTORE" ||
    value === "RECOVERY_REFUSED_NO_SEAT"
    ? value
    : null;
}

/** The one question a row answers for a traveller: may this finger take it back. */
export function binMayRestore(row: ScopedBinRow): boolean {
  return binRecovery(row) === "RECOVERY_ALLOWED";
}

/** A binned trip, typed from the contract rather than re-declared here: slice 1 shipped
 *  `RecycleBinJourney` into the generated client (the schema of that name in
 *  `components["schemas"]`), so the
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

/** The bin as one seat sees it (ORD-0531). No caller name, no call -- see the panel's 无身份 state. */
export function readScopedDiscardedJourneys(travelerId: string): Promise<ScopedBinRow[]> {
  return request<ScopedBinRow[]>(trashScopePath(travelerId));
}

/** The 找回 write. Returns the trip as the server now holds it (`response_model=JourneySchema`). */
export function undeleteJourney(journeyId: string): Promise<{ id: string; title: string }> {
  return request<{ id: string; title: string }>(
    `/journeys/${encodeURIComponent(journeyId)}/undelete`,
    { method: "POST" },
  );
}

/**
 * ORD-0535 -- one discard or one 找回, as the audit projection answers it.
 *
 * `GET /api/v1/journeys/{id}/history` is an existing door and this screen only reads it: the door
 * passes an action it does not recognise straight through as `event_type`, and both
 * `journey.deleted` and `journey.undeleted` are unrecognised, so the sequence arrives in the
 * server's own order (`order_by(AuditLog.created_at)`) without anything here re-sorting it.
 * `deleted_at` is what the discard wrote into the log from ORD-0535 onward; rows older than that
 * carry none, which is why the screen has a separate sentence for the log's own moment.
 */
export type DiscardEvent = {
  event_type: string;
  timestamp: string | null;
  deleted_at: string | null;
};

export async function readDiscardSequence(journeyId: string): Promise<DiscardEvent[]> {
  const rows = await request<Array<Record<string, unknown>>>(
    `/journeys/${encodeURIComponent(journeyId)}/history`,
  );
  return rows
    .filter((row) => row["event_type"] === "journey.deleted" || row["event_type"] === "journey.undeleted")
    .map((row) => {
      const facts = (row["facts"] ?? {}) as Record<string, unknown>;
      return {
        event_type: String(row["event_type"]),
        timestamp: typeof row["timestamp"] === "string" ? String(row["timestamp"]) : null,
        deleted_at: typeof facts["deleted_at"] === "string" ? String(facts["deleted_at"]) : null,
      };
    });
}

/**
 * Which condition refused, in the server's own token.
 *
 * ORD-0465: the reading itself moved to `lib/rejectionFacts.ts`, where the second screen that needs it
 * can reach it without importing this feature. It is re-exported under the name this panel already
 * imports, so the one copy stays the one copy and nothing here changed what it answers.
 */
export { refusalReasonCode };

export { RejectionWithFacts };
