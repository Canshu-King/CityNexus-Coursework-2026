/* ORD-0476 slice 2 -- the footprint client: the two doors that are on the line.
 *
 * Paths carry the `/api/v1` prefix the runtime mounts, not the contract's own (`contracts/openapi.yaml`
 * lists `/journeys/{journey_id}/checkins`, and `tools/verify_openapi_parity.py` maps one onto the
 * other).  This is the trap the sibling order fell into one round ago: fetching the contract's path
 * gets the SPA fallback -- a 200 whose body is HTML -- and the screen reads that as its own failure.
 */

import type { components } from "../../generated/api";

export type CheckinStamp = components["schemas"]["JourneyCheckin"];

export const CHECKINS_PATH = (journeyId: string) =>
  `/api/v1/journeys/${encodeURIComponent(journeyId)}/checkins`;

/** The refusals this screen can say something honest about, read out of `error.reason_code`. */
export type CheckinRefusal =
  | "CHECKIN_DUPLICATE_STAMP"
  | "CHECKIN_JOURNEY_IN_RECYCLE_BIN"
  | "CHECKIN_ACTIVITY_NOT_IN_JOURNEY"
  | "CHECKIN_ACTIVITY_UNKNOWN"
  | "CHECKIN_PLACE_UNKNOWN"
  | "CHECKIN_TARGET_REQUIRED"
  | "CHECKIN_TARGET_AMBIGUOUS"
  /* ORD-0500: the journey leg used to answer through the shared `require()`, which named nothing.
     Now it names itself, so the union has to know the name -- and the switch in `checkinCopy` has a
     sentence for it, which is the pair that keeps a new code from landing as `unexpected`. */
  | "CHECKIN_JOURNEY_UNKNOWN"
  | "UNRECOGNISED";

/**
 * ORD-0500: what a refusal actually said, kept whole enough for a screen to point at.
 *
 * The first cut of this module answered every refusal with `{refusal, status: 0}` and dropped
 * `error.details`, so the duplicate door -- which sends `existing_checkin_id` and
 * `existing_stamped_at` precisely so the traveller can be told WHICH stamp won -- could only ever
 * say "already stamped" in the abstract, and no caller could believe the status.  `status` is now
 * the status the door answered with, and `existingStampedAt` is the server's own instant, never a
 * device clock.
 *
 * The id is deliberately not carried through: it is a uuid, and ORD-0251/ORD-0339 keep machine
 * words out of both the painted and the announced layer.  The moment is the half a person can act
 * on, and the row's own 已打卡 state re-reads from `GET /checkins`, so nothing needs the id.
 */
export type CheckinRefusalFacts = {
  refusal: CheckinRefusal;
  status: number;
  existingStampedAt: string | null;
  /** The server's own recitable receipt, when this answer carried one. */
  receipt: string | null;
};

export type StampOutcome =
  | { kind: "stamped"; stamp: CheckinStamp }
  /* The code is kept as a *value this module switches on*, never rendered: an attribute slot counts
     as DOM under the exposure rule, and a machine token on a Chinese screen is the defect this
     project keeps having to fix. The screen answers with its own dictionary sentence. */
  | { kind: "refused"; facts: CheckinRefusalFacts; refusal: CheckinRefusal; status: number }
  | { kind: "unreachable" };

export type ReadOutcome =
  | { kind: "read"; stamps: CheckinStamp[] }
  | { kind: "refused"; facts: CheckinRefusalFacts; refusal: CheckinRefusal; status: number }
  | { kind: "unreachable" };

async function call(path: string, init?: RequestInit) {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    return { ok: response.ok, status: response.status, body: null };
  }
  return { ok: response.ok, status: response.status, body };
}

const KNOWN_REFUSALS: CheckinRefusal[] = [
  "CHECKIN_DUPLICATE_STAMP",
  "CHECKIN_JOURNEY_IN_RECYCLE_BIN",
  "CHECKIN_ACTIVITY_NOT_IN_JOURNEY",
  "CHECKIN_ACTIVITY_UNKNOWN",
  "CHECKIN_PLACE_UNKNOWN",
  "CHECKIN_TARGET_REQUIRED",
  "CHECKIN_TARGET_AMBIGUOUS",
  "CHECKIN_JOURNEY_UNKNOWN",
];

/**
 * The refusal as the door said it: the name, the status it answered with, and the moment it named.
 *
 * The first cut of this function returned `status: 0` for every refusal and dropped
 * `error.details`, so the duplicate door -- which sends `existing_checkin_id` and
 * `existing_stamped_at` precisely so a traveller can be told WHICH stamp won -- could only ever say
 * "already stamped" in the abstract, and no caller could believe the status it was handed.
 *
 * The id is deliberately not carried through: it is a uuid, and ORD-0251/ORD-0339 keep machine
 * words out of the painted and the announced layer.  The moment is the half a person can act on,
 * and the row's own 已打卡 state re-reads from `GET /checkins`, so nothing here needs the id.
 */
function factsOf(body: unknown, status: number): CheckinRefusalFacts {
  const error = (body as { error?: Record<string, unknown> } | null)?.error ?? {};
  const code = typeof error.reason_code === "string" ? error.reason_code : "";
  const details = (error.details ?? {}) as Record<string, unknown>;
  const stamped = details.existing_stamped_at;
  const receipt = error.error_receipt_id;
  return {
    refusal: (KNOWN_REFUSALS as string[]).includes(code)
      ? (code as CheckinRefusal)
      : "UNRECOGNISED",
    status,
    existingStampedAt: typeof stamped === "string" && stamped ? stamped : null,
    receipt: typeof receipt === "string" && receipt ? receipt : null,
  };
}

/**
 * One construction site for a refusal. `facts` is the whole answer; `refusal` and `status` are
 * read out of it here rather than stored beside it, because ORD-0530's footprint panel switches
 * on `answer.refusal` and this module must keep answering that name.
 */
function refused(
  body: unknown,
  status: number,
): { kind: "refused"; facts: CheckinRefusalFacts; refusal: CheckinRefusal; status: number } {
  const facts = factsOf(body, status);
  return { kind: "refused", facts, refusal: facts.refusal, status: facts.status };
}
/** One tap on one stop. Nothing here is written locally: a stamp exists when the door says 201. */
export async function stampStop(journeyId: string, activityId: string): Promise<StampOutcome> {
  try {
    const answer = await call(CHECKINS_PATH(journeyId), {
      method: "POST",
      body: JSON.stringify({ activity_id: activityId }),
    });
    if (answer.ok && answer.body) {
      return { kind: "stamped", stamp: answer.body as CheckinStamp };
    }
    return refused(answer.body, answer.status);
  } catch {
    return { kind: "unreachable" };
  }
}

/** The trip's footprints, as the server holds them. */
export async function readCheckins(journeyId: string): Promise<ReadOutcome> {
  try {
    const answer = await call(CHECKINS_PATH(journeyId));
    if (answer.ok && Array.isArray(answer.body)) {
      return { kind: "read", stamps: answer.body as CheckinStamp[] };
    }
    return refused(answer.body, answer.status);
  } catch {
    return { kind: "unreachable" };
  }
}

/**
 * Which words a stamp points at. The server resolves the target on every read, so a stamp whose
 * activity has since been removed from the plan answers `activity_title: null` -- and this says
 * "the stop is gone" rather than showing an empty cell or reusing the place's name for it.
 */
export function stampLabel(stamp: CheckinStamp): string | null {
  if (stamp.target_kind === "ACTIVITY") return stamp.activity_title;
  return stamp.place_name;
}
