import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";
import { namedCodeIn } from "../../lib/rawFacts";

/**
 * ORD-0450 -- the two writes the 待规划 screen performs, both through doors that are already on the
 * line.  Nothing here declares a route: the compose call is the one ORD-0435's discovery batch uses
 * (`backend/app/main.py`), and the adopt call is `POST /journeys/{id}/plans/{version}/select`,
 * which `frontend/src/api/citynexus.ts` has been calling for a long time.
 *
 * The read-back is not decoration.  ORD-0435's own reason for existing was that a tick which only
 * lives in component state looks identical on screen and means nothing, so the receipt this file
 * returns is built from `GET /api/v1/plans/{plan_version_id}` -- what the row store holds after the
 * write -- and it reports the places the planner did *not* place as `missing` rather than as success.
 */

/** Every `V13_*` name this call path can answer with, read off the tree rather than guessed: the
 *  raise sites in `compose_authoritative_multiday` and its gate.  Grouped into the four things a
 *  traveller can act on -- the day is too tight, a protected commitment is the obstacle, no version
 *  is feasible, or something else this device cannot classify. */
const CODE_FAMILY = {
  V13_ACTIVITY_OUTSIDE_DEPARTURE_WINDOW: "WINDOW",
  V13_CHECK_OUT_DEPARTURE_BUFFER: "WINDOW",
  V13_INVALID_ANCHOR_WINDOW: "WINDOW",
  V13_JOURNEY_DAYS_OUT_OF_BOUNDS: "WINDOW",
  V13_COMMITMENT_ANCHOR_TRAVEL_BUFFER: "COMMITMENT",
  V13_COMMITMENT_ANCHOR_TOO_EARLY: "COMMITMENT",
  V13_COMMITMENT_ANCHOR_DAY_CONFLICT: "COMMITMENT",
  V13_COMMITMENT_OUTSIDE_OPENING_HOURS: "COMMITMENT",
  V13_C4_ANCHOR_NOT_IN_INPUT: "COMMITMENT",
  V13_DISCOVERY_COMPOSE_EMPTY: "NO_PLAN",
  V13_DISCOVERY_NO_FEASIBLE_PLACE_SET: "NO_PLAN",
  V13_DISCOVERY_NO_FEASIBLE_PLAN: "NO_PLAN",
  V13_AUTHORITATIVE_BOUNDED_MULTIDAY: "NO_PLAN",
  V13_STAY_CANDIDATE_UNAVAILABLE: "NO_PLAN",
  V13_STAY_CHECK_IN_OUTSIDE_JOURNEY: "NO_PLAN",
  V13_DAY_TRIP_CANNOT_SELECT_STAY: "NO_PLAN",
  V13_FULL_GROUNDING_MISSING_MOBILITY_PAIR: "NO_PLAN",
} as const satisfies Record<string, "WINDOW" | "COMMITMENT" | "NO_PLAN">;

const COMPOSE_CODES = Object.keys(CODE_FAMILY);

export type ComposeFamily = "WINDOW" | "COMMITMENT" | "NO_PLAN" | "OTHER";

/** Which named condition refused, as this device read it -- for the technical-details face only.
 *  It is a rejection code from the contract's `V13NamedRejection`, not a credential, so the field
 *  says `rejection_code` (ORD-0467: it used to be called `token`, which is what made the credential
 *  gate read this line as a secret). */
export type ComposeRefusal = { family: ComposeFamily; rejection_code: string | null };

/** Compose answered without naming a plan. Carries no HTTP facts because there were none to read. */
export class ComposeEmptyError extends Error {}

export interface ComposedPlanReadback {
  id: string;
  version_no: number | null;
  status: string | null;
  stop_count: number;
  activities: Array<{ place_id?: string | null; start_at: string; title?: string }>;
}

export interface PlaceOnADayResult {
  composed: { plan_id: string; version_no: number | null; status: string | null; stop_count: number };
  readback: ComposedPlanReadback;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await rejectionWithFacts(response);
  const text = await response.text();
  return JSON.parse(text) as T;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw await rejectionWithFacts(response);
  return (await response.json()) as T;
}

/** The batch the planner is asked to place. `candidate_count: 1` is what the landed batch sends: one
 *  proposed version per press, so a second tick cannot quietly accumulate plans. */
export async function placeOnADay(
  journeyId: string,
  placeIds: string[],
): Promise<PlaceOnADayResult> {
  const composed = await postJson<{ top_k?: Array<{ id?: string }> }>(
    `/journeys/${encodeURIComponent(journeyId)}/plans/authoritative-compose`,
    { place_ids: placeIds, candidate_count: 1 },
  );
  const proposedId = composed.top_k?.[0]?.id;
  if (!proposedId) {
    /* The answer was well-formed and named no plan.  That is its own fact, so it gets its own error
       type rather than a fabricated `RejectionWithFacts` with measurements this device never took. */
    throw new ComposeEmptyError("V13_DISCOVERY_COMPOSE_EMPTY");
  }
  const readback = await getJson<ComposedPlanReadback>(`/plans/${encodeURIComponent(proposedId)}`);
  return {
    composed: {
      plan_id: String(readback.id ?? proposedId),
      version_no: typeof readback.version_no === "number" ? readback.version_no : null,
      status: readback.status ?? null,
      stop_count: (readback.activities ?? []).length,
    },
    readback,
  };
}

/** Adoption.  This retires the plan the trip may be executing, which is why the screen asks for it as
 *  its own press and names the consequence before the button. */
export async function adoptComposedPlan(journeyId: string, planVersionId: string): Promise<void> {
  await postJson<unknown>(
    `/journeys/${encodeURIComponent(journeyId)}/plans/${encodeURIComponent(planVersionId)}/select`,
    {},
  );
}

/** Which of the four things a traveller can act on refused, plus the server's own named
 *  rejection for the details face.  Both envelope shapes are scanned (`error.code`, and a code
 *  prefixed inside the message), because the two raise sites differ. */
export function composeRefusal(cause: unknown): ComposeRefusal {
  if (cause instanceof ComposeEmptyError) {
    return { family: "NO_PLAN", rejection_code: "V13_DISCOVERY_COMPOSE_EMPTY" };
  }
  if (!(cause instanceof RejectionWithFacts)) return { family: "OTHER", rejection_code: null };
  const envelope = cause.envelope;
  const parts: string[] = [];
  if (typeof envelope === "object" && envelope !== null) {
    const record = envelope as Record<string, unknown>;
    const error = record.error;
    if (typeof error === "object" && error !== null) {
      const code = (error as Record<string, unknown>).code;
      if (typeof code === "string") parts.push(code);
      const message = (error as Record<string, unknown>).message;
      if (typeof message === "string") parts.push(message);
    }
    if (typeof record.detail === "string") parts.push(record.detail);
  }
  const found = namedCodeIn(parts.join(" "), COMPOSE_CODES);
  if (!found) return { family: "OTHER", rejection_code: null };
  const family = CODE_FAMILY[found as keyof typeof CODE_FAMILY];
  return { family, rejection_code: found };
}

export { RejectionWithFacts };
