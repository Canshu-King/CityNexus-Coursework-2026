import type { V8Data } from "../../app/v8Types";

/**
 * ORD-0445 -- the 待规划 set, derived only from reads this app already makes.
 *
 * The set is one thing, and it has a real field behind it: a place this trip captured
 * (`real_place_candidates`, `backend/app/db/models.py`) that no activity of the chosen plan
 * holds.  `GET /api/v1/journeys/{journey_id}/provider-mesh` (`backend/app/main.py`) answers the
 * captured half, `GET /places?city_code=` (`backend/app/main.py`) answers the category word, and
 * the shell already holds the plan.  No new table, no new field, no new write door.
 *
 * One thing this file deliberately does NOT do is guess at a second kind of "no day".  An activity
 * always has a day here, because a day is *derived from* `start_at` and nothing else --
 * `features/trip/TripItinerary.tsx` buckets every activity by that instant and renders every
 * bucket, including any whose date lies outside `journey.start_at .. journey.end_at`.  So an activity
 * whose date fell out of the trip's window would be shown as a day by the screen next door while this
 * one called it "没有排在某一天" -- two answers about the same stop.  The order's own instruction is to
 * report rather than invent a field, so the lane reports instead: this build has no representation of
 * "a place with no day" other than the captured-but-unplaced row, and the date door even refuses to
 * produce one (`backend/app/main.py`, answered at `:5210`; measured 2026-09-29 on this lane's own
 * copy, `D:/moodle/_qoder_n2b_ord0445_20260929/prep_receipt_r1.json`).
 *
 * Nor does the screen offer to put a place onto a day: this server has no such write
 * (`activities` move only through execution -- `backend/app/main.py`, `:7226`, `:7231`), so a
 * button for it would be copy exceeding mechanism, which ORD-0385 forbids.
 */

/** One captured place, as this screen reads it.  Declared here rather than imported from the
 *  generated client: the route answers with a hand-built dict (`backend/app/main.py`), so what
 *  this device got is what is true, and the guard in `pendingSetReads.ts` decides whether the answer
 *  really was a list. */
export interface PendingCandidate {
  id: string;
  name: string;
  category: string | null;
  provider_key: string | null;
  status: string | null;
  materialized_place_id: string | null;
  captured_at: string | null;
}

/** The two place columns this screen needs (`id` to join a plan stop, `category` to filter on). */
export interface PendingPlaceRow {
  id: string;
  category: string | null;
}

export interface PendingRow {
  key: string;
  name: string;
  /** The server's own category token, or null when no read named one. */
  categoryToken: string | null;
  /** The local date the row's own capture happened on, from the server's `captured_at`. */
  capturedDay: string | null;
  /** The trip's own date range in its own zone, so 「这一程」 on screen is checkable. */
  tripStart: string | null;
  tripEnd: string | null;
  stateToken: string | null;
  providerKey: string | null;
  placeId: string | null;
}

export interface PendingDerivation {
  rows: PendingRow[];
  /** Denominators the screen prints, so a zero is a reading and not a silence. */
  counts: {
    activities_read: number;
    candidates_read: number;
    places_read: number | null;
    /** Places the plan already holds on some day -- the set is what is left of the captures. */
    placed_place_ids: number;
    /** Captures the plan already holds, i.e. counted out of the set. */
    candidates_placed: number;
  };
  journeyId: string | null;
  timezone: string | null;
  tripStart: string | null;
  tripEnd: string | null;
  /** Whether the shell handed over a chosen plan at all -- a named state, not an empty set. */
  hasActivePlan: boolean;
}

/**
 * The traveller's own calendar day for an instant.  Same rule as the itinerary's, and deliberately
 * not imported from it: `TripItinerary.tsx` keeps it local, and reaching into another lane's
 * screen for a helper would make this panel's count depend on a file this order may not touch.
 */
export function localDateKey(iso: string, timeZone: string): string | null {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return null;
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(parsed));
  } catch {
    /* A zone this device does not know is the server's fact to report, not a licence to fall back to
       the phone's zone and print a day the traveller never chose. */
    return null;
  }
}

/**
 * The whole derivation, in one pure function, so the screen's numbers are checkable without a
 * browser.  Order is the server's own: candidates come back `captured_at desc, name`
 * (`backend/app/main.py`) and this screen renders them as answered -- the same rule the
 * recycle-bin screen follows (`features/trash/journeyTrash.ts`).  Nothing here re-sorts.
 */
export function derivePendingSet(input: {
  data: V8Data | null;
  candidates: PendingCandidate[] | null;
  places: PendingPlaceRow[] | null;
}): PendingDerivation {
  const data = input.data;
  const journey = data?.journey;
  const timezone = journey?.timezone || null;
  const plan = data?.active_plan ?? null;
  const activities = plan?.activities ?? [];
  const candidates = input.candidates ?? [];

  const tripStart = journey && timezone ? localDateKey(journey.start_at, timezone) : null;
  const tripEnd = journey && timezone ? localDateKey(journey.end_at, timezone) : null;

  /* Which places some day already holds.  Both columns count: a plan stop names its place in
     `place_id`, and a capture names the row it materialised into
     (`backend/app/db/models.py`), so the join is place-to-place, never name-to-name. */
  const placedPlaceIds = new Set<string>();
  for (const activity of activities) {
    const id = activity.place_id;
    if (typeof id === "string" && id.trim()) placedPlaceIds.add(id);
  }

  const categoryByPlaceId = new Map<string, string | null>();
  for (const place of input.places ?? []) {
    if (place?.id) categoryByPlaceId.set(place.id, place.category);
  }

  let placed = 0;
  const rows: PendingRow[] = [];
  for (const candidate of candidates) {
    const materialised = candidate.materialized_place_id;
    if (typeof materialised === "string" && placedPlaceIds.has(materialised)) {
      placed += 1;
      continue;
    }
    /* The category comes from the same server row the screen is listing; when the capture named
       none, the join onto the place list is the only other honest source, and failing that the row
       says it has no category instead of being filed somewhere. */
    const categoryFromJoin =
      typeof materialised === "string" ? categoryByPlaceId.get(materialised) ?? null : null;
    rows.push({
      key: `cap:${candidate.id}`,
      name: candidate.name,
      categoryToken: candidate.category ?? categoryFromJoin,
      capturedDay: candidate.captured_at && timezone
        ? localDateKey(candidate.captured_at, timezone)
        : null,
      tripStart,
      tripEnd,
      stateToken: candidate.status,
      providerKey: candidate.provider_key,
      placeId: materialised,
    });
  }

  return {
    rows,
    counts: {
      activities_read: activities.length,
      candidates_read: candidates.length,
      places_read: input.places === null ? null : (input.places ?? []).length,
      placed_place_ids: placedPlaceIds.size,
      candidates_placed: placed,
    },
    journeyId: journey?.id ?? null,
    timezone,
    tripStart,
    tripEnd,
    hasActivePlan: plan !== null,
  };
}

/** The filter facets, in the order the rows first name them, each with its own count.
 *  Built from the rows, never from a list this file keeps, so a facet cannot appear with zero rows. */
export function facetsOf(rows: PendingRow[]): Array<{ token: string; count: number }> {
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const row of rows) {
    const token = row.categoryToken;
    if (token === null) continue;
    if (!counts.has(token)) {
      counts.set(token, 0);
      order.push(token);
    }
    counts.set(token, (counts.get(token) ?? 0) + 1);
  }
  return order.map((token) => ({ token, count: counts.get(token) ?? 0 }));
}

export function rowsForFilter(rows: PendingRow[], choice: string): PendingRow[] {
  if (choice === "all") return rows;
  return rows.filter((row) => row.categoryToken === choice);
}

/* ------------------------------------------------------------------ ORD-0450: putting a pick on a day
 *
 * The write itself is not invented here.  It is the channel ORD-0435 landed:
 * `POST /api/v1/journeys/{journey_id}/plans/authoritative-compose` (`backend/app/main.py`) taking
 * `AuthoritativeComposeRequest` (`backend/app/schemas.py` -- `place_ids`, 2..12), followed by
 * `GET /api/v1/plans/{plan_version_id}` to read back what the planner actually wrote.  Adoption is the
 * other door that already exists, `POST /journeys/{id}/plans/{version}/select`
 * (`frontend/src/api/citynexus.ts` calls it today).  This order opens neither.
 *
 * What the two functions below decide is only *which place ids go in*, and the rule is "lose nothing":
 * every place the chosen plan already holds travels with the batch, plus the ticks the traveller made.
 * Sending protected commitments alone -- which is what the discovery batch does, because it is building
 * a plan from scratch -- would quietly drop the ordinary stops of a plan that already exists, and a
 * screen that adds a place must not be able to remove five.
 */

export type PendingWriteBlock =
  | "NOTHING_PICKED"
  | "PICK_HAS_NO_PLACE_ROW"
  | "SET_TOO_SMALL"
  | "SET_TOO_LARGE"
  | null;

export interface PendingWriteInput {
  /** What `AuthoritativeComposeRequest.place_ids` will carry, in plan order then tick order. */
  place_ids: string[];
  picked_place_ids: string[];
  planned_place_ids: string[];
  protected_place_ids: string[];
  ok: boolean;
  blocked_by: PendingWriteBlock;
  /** The server's own bound, read from the schema rather than repeated as a local belief. */
  max_places: number;
}

function uniqueOrdered(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values) {
    if (!seen.has(value)) {
      seen.add(value);
      out.push(value);
    }
  }
  return out;
}

export function composeInputFor(args: { data: V8Data | null; picked: PendingRow[] }): PendingWriteInput {
  const activities = args.data?.active_plan?.activities ?? [];
  const planned: string[] = [];
  const protectedPlaces: string[] = [];
  for (const activity of activities) {
    const id = typeof activity.place_id === "string" && activity.place_id.trim() ? activity.place_id : null;
    if (!id) continue;
    planned.push(id);
    if (activity.protected) protectedPlaces.push(id);
  }
  const pickedPlaces: string[] = [];
  let pickWithoutRow = false;
  for (const row of args.picked) {
    if (row.placeId) pickedPlaces.push(row.placeId);
    else pickWithoutRow = true;
  }

  const placeIds = uniqueOrdered([...planned, ...pickedPlaces]);
  let blocked: PendingWriteBlock = null;
  if (args.picked.length === 0) blocked = "NOTHING_PICKED";
  else if (pickWithoutRow) blocked = "PICK_HAS_NO_PLACE_ROW";
  else if (placeIds.length < 2) blocked = "SET_TOO_SMALL";
  else if (placeIds.length > MAX_COMPOSE_PLACES) blocked = "SET_TOO_LARGE";

  return {
    place_ids: placeIds,
    picked_place_ids: uniqueOrdered(pickedPlaces),
    planned_place_ids: uniqueOrdered(planned),
    protected_place_ids: uniqueOrdered(protectedPlaces),
    ok: blocked === null,
    blocked_by: blocked,
    max_places: MAX_COMPOSE_PLACES,
  };
}

/** `max_length=12` on the request model (`backend/app/schemas.py`); the screen names this number
 *  as the server's bound and refuses locally rather than letting a 422 arrive as a surprise. */
export const MAX_COMPOSE_PLACES = 12;

/** Which day a read-back stop carries, in the traveller's own zone -- the same derivation the
 *  itinerary uses, so the day named here is the day that screen will draw. */
export function daysOfReadback(
  activities: Array<{ place_id?: string | null; start_at: string; title?: string }>,
  wanted: string[],
  timezone: string | null,
): { landed: Array<{ place_id: string; title: string; day: string | null }>; missing: string[]; days: string[] } {
  const landed: Array<{ place_id: string; title: string; day: string | null }> = [];
  for (const activity of activities) {
    const id = activity.place_id ? String(activity.place_id) : null;
    if (!id || !wanted.includes(id)) continue;
    landed.push({
      place_id: id,
      title: activity.title ?? "",
      day: timezone ? localDateKey(activity.start_at, timezone) : null,
    });
  }
  const held = new Set(landed.map((item) => item.place_id));
  return {
    landed,
    missing: wanted.filter((id) => !held.has(id)),
    days: uniqueOrdered(landed.map((item) => item.day).filter((day): day is string => !!day)).sort(),
  };
}
