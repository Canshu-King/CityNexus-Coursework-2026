import type { components } from "../../generated/api";
import {
  rejectionWithFacts,
  refusalDetails,
  refusalReasonCode,
  RejectionWithFacts,
} from "../../lib/rejectionFacts";

/**
 * ORD-0520 中止 -- the one door this screen uses, kept in its own file so the panel renders and
 * cannot hide what it asked the server for (same shape as `features/trash/journeyTrash.ts`).
 *
 * The door is on the line: `POST /api/v1/journeys/{journey_id}/abort`, declared in
 * `contracts/openapi.yaml` and generated into `frontend/src/generated/api.ts`, writes
 * `journeys.status` = CANCELLED with `abort_reason` and `aborted_at` on the same row.  The server
 * answers the live-screen judgement as `live_view`, so nothing in this file filters by status --
 * the screen reads the server's field and renders what it says
 * (`backend/app/services.py` -- `journey_live_view` is the only place that decides).
 *
 * 中止 is not 丢弃.  This door touches no `deleted_at`, so an aborted trip is still in the trip list
 * and is not in the recycle bin; the words 找回 and 丢弃 belong to the bin's own doors, 还原 to plan
 * version history, and 恢复 to autonomous recovery.  There is no un-abort door on this server, so no
 * sentence in `journeyAbortCopy.ts` may offer one.
 */
export const JOURNEY_ABORT_DOOR = "/journeys/{journey_id}/abort";

export type JourneyAbortRequest = components["schemas"]["JourneyAbort"];
/** The trip as the server holds it after the write, read from the contract rather than re-declared. */
export type AbortReadbackJourney = components["schemas"]["Journey"];
export type JourneyLiveView = components["schemas"]["JourneyLiveView"];

function abortPath(journeyId: string): string {
  return JOURNEY_ABORT_DOOR.replace(
    "{journey_id}",
    encodeURIComponent(journeyId),
  );
}

/**
 * The 中止 write.  `actorTravelerId` is the identity this request speaks for -- the trip's own
 * traveller, which is all this shell holds; the server compares it against the row and answers
 * `ABORT_ACTOR_NOT_OWNER` when they differ, rather than pretending the write happened.
 */
export async function abortJourney(
  journeyId: string,
  actorTravelerId: string,
  reason: string,
): Promise<AbortReadbackJourney> {
  const body: JourneyAbortRequest = { reason, actor_traveler_id: actorTravelerId };
  const response = await fetch(`/api/v1${abortPath(journeyId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await rejectionWithFacts(response);
  return (await response.json()) as AbortReadbackJourney;
}

export { refusalDetails, refusalReasonCode, RejectionWithFacts };
