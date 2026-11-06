import type { components, paths } from "../../generated/api";
import { classifyReadFailure, type Attribution } from "../../lib/failureAttribution";
import type { RawFacts } from "../../lib/rawFacts";
import {
  refusalDetails,
  refusalReasonCode,
  rejectionWithFacts,
  RejectionWithFacts,
} from "../../lib/rejectionFacts";

/**
 * ORD-0465 -- the one door the share-receiving screen reads, and the one reading of its answer.
 *
 * There is exactly one door here on purpose. The screen needs a trip's days and its stops, and every
 * other journey door on this tree is either one that writes while it answers
 * (`GET /api/v1/journeys/{journey_id}/experience` -- the reason this project has a documented 500 from
 * opening a page) or one that has never heard of a share grant (`GET /api/v1/journeys/{id}`,
 * `/plans/{id}`). A screen assembled from those would keep filling in after the owner withdrew the
 * link or the deadline passed, which is the one thing a grant must not allow. So `plan_state` and
 * `active_plan` ride on this answer (`backend/app/schemas.py`) and nowhere else.
 *
 * Opening this address sends a GET and nothing else. No traveller is registered, no journey is
 * created, no demo scenario is claimed -- the recipient did not ask for any of that and this device
 * has no business deciding they did.
 */

/**
 * The door's 200 body, read off the generated operation rather than re-declared here.  This door has
 * no `response_model` on the server -- ORD-0466's contract is that `shared_plan` is ABSENT on a
 * journey-level grant, and a model with a default would write a null into that key and read as "this
 * grant covered a version and it went missing" -- so the generated client type for this response is
 * the only place the body is described on this side, and a hand-copy here would be a second shape.
 */
export type SharedJourneyRead =
  paths["/shared/journeys/{token}"]["get"]["responses"][200]["content"]["application/json"];
export type SharedPlan = components["schemas"]["SharedPlan"];
export type SharedStop = components["schemas"]["SharedStop"];
export type SharedActivityCoordinate = components["schemas"]["SharedActivityCoordinate"];

/** The door's path, kept as one constant the way `journeyTrash.ts` keeps its two. */
export const SHARED_JOURNEY_DOOR = "/shared/journeys/";

/**
 * Every named refusal this door can answer with, and the state each one puts on screen.
 *
 * The list is the contract's list, read off `contracts/openapi.yaml` and the raise sites in
 * `backend/app/main.py` beside `read_shared_journey`, not off what the screen happens to handle: four
 * of these are the states the order names, and the other five exist anyway -- `PRIVATE`,
 * `INVITEES_ONLY` and an unreadable deadline are all answers a traveller can be given today. Dropping
 * them from the table would not make them unreachable; it would only make them land in
 * `UNNAMED_REFUSAL`, which says something different and would be a lie about a server that did name
 * its reason.
 */
export const SHARED_REFUSAL_STATES = {
  SHARE_GRANT_UNKNOWN: "token_unknown",
  SHARE_GRANT_REVOKED: "revoked",
  SHARE_GRANT_EXPIRED: "expired",
  SHARE_GRANT_EXPIRY_UNREADABLE: "expiry_unreadable",
  SHARE_GRANT_NOT_LINK_READABLE: "not_link_readable",
  SHARE_GRANT_MEMBERSHIP_REQUIRED: "membership_required",
  SHARE_GRANT_NOT_A_MEMBER: "not_a_member",
  SHARE_GRANT_JOURNEY_GONE: "journey_gone",
  SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN: "in_recycle_bin",
} as const satisfies Record<string, string>;

export type SharedRefusalState = (typeof SHARED_REFUSAL_STATES)[keyof typeof SHARED_REFUSAL_STATES];

/**
 * What the screen shows, in six arms, each with a sentence of its own.
 *
 * `NO_TOKEN_IN_ADDRESS` is the only arm not produced by the server, and it is labelled as such on
 * screen: this device looked at the address the traveller was given and found no token in it, so no
 * request was made and there is no verdict to quote. It is deliberately not one of the four refusal
 * states -- collapsing it into `token_unknown` would blame a stranger's clipboard for a link that was
 * never assembled.
 */
export type SharedScreenRead =
  | { state: "loading" }
  | { state: "no_token_in_address" }
  | {
      state: "read_failed";
      facts: RawFacts | null;
      /** Classified by `lib/failureAttribution`, the one owner of "network, origin, or nothing" (R14-P). */
      attribution: Attribution;
      /** Always null on this arm, and named so rather than omitted: there is no answer to read a
       *  receipt out of when no answer arrived. The fold says 服务器未给回执号 because nothing came. */
      envelope: null;
    }
  | {
      state: "refused";
      refusal: SharedRefusalState | "unnamed";
      /** The token the server actually sent, which is what the details face quotes. Null when the body
       *  carried no name at all -- and then the screen says it read none rather than picking one. */
      reason_code: string | null;
      details: Record<string, unknown> | null;
      facts: RawFacts | null;
      /** The parsed envelope, carried so `CnxMeasuredFacts` can read the server's own receipt number
       *  out of it. Dropping it here is what made that line answer "no server receipt" about a body
       *  that did carry one -- ORD-0474's fourth row. */
      envelope: unknown;
    }
  | { state: "ready"; answer: SharedJourneyRead };

/**
 * The one read this screen makes, shaped the way `journeyTrash.ts` and `accessDoors.ts` shape theirs,
 * and deliberately returning a state rather than throwing: the caller's job is to say which of six
 * things happened, and a throw would push that decision into whichever `catch` happened to be written
 * nearest the call.
 */
export function askSharedJourney(token: string): Promise<SharedScreenRead> {
  return fetch(`/api/v1${SHARED_JOURNEY_DOOR}${encodeURIComponent(token)}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  })
    .then(async (response): Promise<SharedScreenRead> => {
      if (response.ok) {
        return { state: "ready", answer: (await response.json()) as SharedJourneyRead };
      }
      throw await rejectionWithFacts(response);
    })
    .catch((cause: unknown): SharedScreenRead => {
      if (cause instanceof RejectionWithFacts) {
        const code = refusalReasonCode(cause);
        return {
          state: "refused",
          refusal: code ? SHARED_REFUSAL_STATES[code as keyof typeof SHARED_REFUSAL_STATES] ?? "unnamed" : "unnamed",
          reason_code: code,
          details: refusalDetails(cause),
          facts: cause.facts,
          envelope: cause.envelope,
        };
      }
      /* A fetch that never got an answer is not a refusal, and it has no code to read. This is the one
         arm that may name the device rather than the server, and it is not allowed to borrow one of
         the four sentences above -- so it carries the classification that `lib/failureAttribution`
         produces, which is R14-P's one owner for "was this the network, the origin, or nothing". */
      return { state: "read_failed", facts: null, envelope: null, attribution: classifyReadFailure(cause) };
    });
}

/**
 * The stops this answer carries, and nothing else.
 *
 * `plan_state` is asked for rather than inferred from `active_plan` being null, because the two null
 * cases are different facts (this trip never had a plan / this trip points at a plan row the store
 * does not hold) and the screen says them with different sentences.
 */
export function sharedStopsOf(answer: SharedJourneyRead): SharedStop[] {
  return answer.plan_state === "PLAN_PRESENT" ? (answer.active_plan?.stops ?? []) : [];
}

export { RejectionWithFacts, refusalReasonCode };
