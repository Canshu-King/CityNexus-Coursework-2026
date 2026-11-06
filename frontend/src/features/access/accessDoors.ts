/**
 * ORD-0436 -- the doors this screen reads, in one file, so what it claims and what it asked the
 * server are separable.
 *
 * Two of them are on the line today and are named by their server-side position:
 *  - `GET /api/v1/journeys` -- `backend/app/main.py`, the list door ORD-0383 slice 1 kept.
 *  - `GET /api/v1/journeys/{id}/agent-feed` -- `backend/app/main.py`, the activity stream that
 *    reads `audit_logs` for one trip.
 * The sharing state belongs to ORD-0434 and is asked as three doors (`SHARING_DOOR_SUFFIXES` below).
 * None of them answers at this tip. A missing answer and an empty answer are different facts, so the
 * screen reports the doors it asked and their status rather than showing an empty roster, which would
 * read as "nobody shares this trip" -- a claim this tree has no source for.
 */
import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";
import type { operations } from "../../generated/api";

export const JOURNEY_LIST_PATH = "/journeys";

/** ORD-0477: which order the list door answers in, taken from the generated client rather than
 *  written out again here -- `operations["listJourneys"]` is generated from the `sort` enum in
 *  `contracts/openapi.yaml`, so a tier the contract renames is a build error on this screen instead
 *  of a control that quietly stops working, and a tier the contract drops cannot survive as a button. */
export type JourneySortTier = NonNullable<
  NonNullable<operations["listJourneys"]["parameters"]["query"]>["sort"]
>;
/** ORD-0518: what this screen calls an order.  `null` is `创建序` -- not a fourth word the door
 *  knows, but the order it answers in when no `sort` is sent at all, which is the only honest way to
 *  offer creation order without changing the door.  Whether the rows really are in that order is
 *  judged from the rows in `journeyOrderVerdict.ts`, never from this value. */
export type JourneyOrderChoice = JourneySortTier | null;
export const ACTIVITY_FEED_SUFFIX = "/agent-feed";
/**
 * The sharing state lives behind three doors, and their names are read off ORD-0434's route set
 * (`review/qoder-0434-share-grants-r1`: `/members`, `/share-grants`, `/acl/roles`), not invented
 * here. None of the three answers at this tip, which is the fact the screen says out loud; an
 * invented `/access` was worse than none, because it named a door nobody is building.
 */
export const SHARING_DOOR_SUFFIXES = ["/members", "/share-grants", "/acl/roles"] as const;

export type JourneySummary = {
  id: string;
  title: string;
  city_code?: string;
  status?: string;
  /** ORD-0518: the departure instant, off the same `Journey` shape the door answers with.  The
   *  screen reads it for one purpose -- to check a `DEPARTURE_DATE` answer against the rows it was
   *  given instead of against the button it pressed.  It is not painted. */
  start_at?: string;
  /** The one person this trip belongs to. Sent by the list door, and the only value this screen is
   *  willing to put in a `created_by` field. */
  traveler_id?: string | null;
};

export type ActivityEvent = {
  id: string;
  timestamp: string;
  event_type: string;
  summary: string;
  actor_type?: string | null;
  actor_id?: string | null;
  /** Whose row an action touched. The feed does not send this today, and the panel says so out loud
   *  rather than filling it from `actor_id` -- who tapped is not who changed. */
  member_name?: string | null;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) throw await rejectionWithFacts(response);
  return (await response.json()) as T;
}

/** The door's path with the tier spelled out in it, so a gate can read the word this screen sent
 *  instead of inferring it from the order the rows happened to come back in.  `null` sends no `sort`
 *  at all, which is the door's creation order -- and the URL says so by not carrying the parameter. */
export function journeyListPath(choice: JourneyOrderChoice): string {
  return choice === null ? JOURNEY_LIST_PATH : `${JOURNEY_LIST_PATH}?sort=${encodeURIComponent(choice)}`;
}

/** ORD-0477: the list, in the order the traveller asked for.  A tier the door does not know is a
 *  422 naming itself (`JOURNEY_SORT_NOT_RECOGNISED`) and no rows at all, which reaches this screen
 *  as a refusal with facts rather than as an empty list -- `access.pickEmpty` stays the sentence for
 *  "there are no trips", and is never the sentence for "you asked wrongly".
 *
 *  ORD-0518: this returns the rows and nothing else. There is deliberately no "the order is X" value
 *  coming from here, because there is nothing on this door that could vouch for one -- no response
 *  header, and no order field in the row shape. What the screen says about the order is decided by
 *  `journeyOrderVerdict.ts` from the rows themselves. */
export function readJourneys(choice: JourneyOrderChoice): Promise<JourneySummary[]> {
  return request<JourneySummary[]>(journeyListPath(choice));
}

/** The server's `summary` is deliberately not trusted for the words on screen: for an action the
 *  projection does not know it derives a capitalised English phrase out of the token
 *  (`backend/app/main.py`), which is a machine name wearing a sentence. The screen keeps the
 *  token in the details face and says its own sentence from `accessVocabulary.ts`. */
export function readActivity(journeyId: string): Promise<ActivityEvent[]> {
  return request<{ events: ActivityEvent[] }>(
    `/journeys/${encodeURIComponent(journeyId)}${ACTIVITY_FEED_SUFFIX}?limit=50`,
  ).then((answer) => answer.events);
}

/** One door this screen asked, with the status it actually answered. `answered: false` is reserved
 *  for the shapes that mean "this server has no such door" (404 / 405 / 501); anything else throws,
 *  so a network failure is never reported as absence. */
export type DoorProbe = { path: string; http_status: number | null; answered: boolean };

export type SharingRead = {
  probes: DoorProbe[];
  /** How many member rows `/members` answered, or `null` when that door did not answer. A count of
   *  rows the server sent, not a quota: this product has no member-limit field to compare it with. */
  rosterSize: number | null;
  /** The `expires_at` values the doors did send. Their contract carries the field on both
   *  `ShareGrant` and `JourneyMember` as `str | None`, so an expiry on screen comes from one of
   *  these rows and never from a duration this file could make up. */
  expiries: string[];
};

const ABSENT_STATUSES = new Set([404, 405, 501]);

async function ask(path: string): Promise<{ status: number; rows: unknown } | null> {
  const response = await fetch(`/api/v1${path}`, { headers: { "Content-Type": "application/json" } });
  if (ABSENT_STATUSES.has(response.status)) return null;
  if (!response.ok) throw await rejectionWithFacts(response);
  return { status: response.status, rows: await response.json() };
}

function expiriesIn(rows: unknown): string[] {
  if (!Array.isArray(rows)) return [];
  return rows
    .map((row) =>
      row && typeof row === "object" ? (row as Record<string, unknown>).expires_at : null,
    )
    .filter((value): value is string => typeof value === "string" && value.length > 0);
}

export async function readSharingDoors(journeyId: string): Promise<SharingRead> {
  const probes: DoorProbe[] = [];
  const expiries: string[] = [];
  let rosterSize: number | null = null;
  for (const suffix of SHARING_DOOR_SUFFIXES) {
    const answer = await ask(`/journeys/${encodeURIComponent(journeyId)}${suffix}`);
    if (answer === null) {
      probes.push({ path: suffix, http_status: null, answered: false });
      continue;
    }
    probes.push({ path: suffix, http_status: answer.status, answered: true });
    expiries.push(...expiriesIn(answer.rows));
    if (suffix === "/members" && Array.isArray(answer.rows)) rosterSize = answer.rows.length;
  }
  return { probes, expiries, rosterSize };
}

export { RejectionWithFacts };

/** -------------------------------------------------------------------------------- ORD-0449 writes
 *  The three doors below are the ones ORD-0434 landed: create a grant, revoke a grant, withdraw a
 *  member. Nothing here invents a fourth -- no visibility write, no invitation-cap door, and no
 *  audit row is claimed, because none of these doors writes `audit_logs` (measured at this tip:
 *  doors writing audit = 31, sharing doors among them = 0).
 */
export type ShareGrant = {
  id: string;
  journey_id: string;
  visibility: string;
  created_by: string;
  expires_at: string | null;
  revoked_at: string | null;
  token: string;
};

export type JourneyMemberRow = {
  id: string;
  traveler_id: string;
  role: string;
  invited_by: string;
  expires_at: string | null;
  joined_at: string | null;
  removed_at: string | null;
  member_state: string;
};

export function readShareGrants(journeyId: string): Promise<ShareGrant[]> {
  return request<ShareGrant[]>(`/journeys/${encodeURIComponent(journeyId)}/share-grants`);
}

/** One ACL row as the server answers it: the role, the capabilities it grants, and the dictionary
 *  key for the sentence that says what the role may do. The key is the server's, so the words stay
 *  in the dictionary that owns them. */
export type RoleRule = {
  role: string;
  capabilities: string[];
  scope_message_key: string;
};

export function readRoleRules(journeyId: string): Promise<RoleRule[]> {
  return request<RoleRule[]>(`/journeys/${encodeURIComponent(journeyId)}/acl/roles`);
}

export function readMembers(journeyId: string): Promise<JourneyMemberRow[]> {
  return request<JourneyMemberRow[]>(`/journeys/${encodeURIComponent(journeyId)}/members`);
}

/** ORD-0527: the journey this device is already on, read the way the shell reads it.
 *
 *  `REDACTED_FOR_PUBLIC_RELEASE` is the shell's own reference (`app/V8Experience.tsx`, and the
 *  `citynexus.v8.*` family named at `lib/failureAttribution.ts`); `features/planning/
 *  PlanRankingPanel.tsx` is the shipped precedent of a panel reading it instead of waiting for a
 *  manual pick.  This screen read nothing, which is why a roster with rows in the store still painted
 *  the empty cell.  The value is only a *reference*: the caller must find it among the rows the list
 *  door answered, because a stale id here is the exact hazard that file documents.
 */
export const JOURNEY_REFERENCE_KEY = "REDACTED_FOR_PUBLIC_RELEASE";

export function readJourneyReference(): string | null {
  try {
    return window.localStorage.getItem(JOURNEY_REFERENCE_KEY);
  } catch {
    // A device that refuses storage is a real state, not a crash: the screen says it does not know
    // which trip it is on, and the trip picker stays the way in.
    return null;
  }
}

/** `created_by` is what this device signs the grant with; the door does not authenticate it, so the
 *  screen only ever sends the trip's own `traveler_id` -- a value it read from the list door -- and
 *  says out loud that this is a self-reported signature, not a verified identity. */
export function createShareGrant(
  journeyId: string,
  grant: { visibility: string; created_by: string; expires_at: string | null },
): Promise<ShareGrant> {
  return request<ShareGrant>(`/journeys/${encodeURIComponent(journeyId)}/share-grants`, {
    method: "POST",
    body: JSON.stringify(grant),
  });
}

export function revokeShareGrant(journeyId: string, grantId: string): Promise<ShareGrant> {
  return request<ShareGrant>(
    `/journeys/${encodeURIComponent(journeyId)}/share-grants/${encodeURIComponent(grantId)}/revoke`,
    { method: "POST" },
  );
}

export function withdrawMember(journeyId: string, memberId: string): Promise<JourneyMemberRow> {
  return request<JourneyMemberRow>(
    `/journeys/${encodeURIComponent(journeyId)}/members/${encodeURIComponent(memberId)}/withdraw`,
    { method: "POST" },
  );
}

/** -------------------------------------------------------------------------------- ORD-0503 writes
 *  The two member doors this order adds, plus the one read door that carries its cap. Their paths are
 *  copied from the server's own decorators (`backend/app/main.py`, `change_journey_member_role` and
 *  `leave_journey_as_self`), not assembled from a guess: `self-exit` deliberately takes no member id,
 *  because a body that could name a second person would make this the withdraw door again.
 */
export type MemberRoleLimits = {
  max_administrators: number;
  admin_count_in_force: number;
  roles: string[];
};

export type TravellerRead = { id: string; display_name: string | null };

export const MEMBER_ROLE_LIMITS_SUFFIX = "/member-role-limits";

export function readMemberRoleLimits(journeyId: string): Promise<MemberRoleLimits> {
  return request<MemberRoleLimits>(
    `/journeys/${encodeURIComponent(journeyId)}${MEMBER_ROLE_LIMITS_SUFFIX}`,
  );
}

export function changeMemberRole(
  journeyId: string,
  memberId: string,
  role: string,
  actorTravelerId: string,
): Promise<JourneyMemberRow> {
  return request<JourneyMemberRow>(
    `/journeys/${encodeURIComponent(journeyId)}/members/${encodeURIComponent(memberId)}/role`,
    { method: "POST", body: JSON.stringify({ role, actor_traveler_id: actorTravelerId }) },
  );
}

export function leaveJourney(journeyId: string, travelerId: string): Promise<JourneyMemberRow> {
  return request<JourneyMemberRow>(
    `/journeys/${encodeURIComponent(journeyId)}/members/self-exit`,
    { method: "POST", body: JSON.stringify({ traveler_id: travelerId }) },
  );
}

/** One person's own name, from the registry door.
 *
 *  The roster needs it because the member row answers with an id, and an id is not something to put
 *  in front of a person.  A 404 here is a real answer -- the registry has no row for that id -- and it
 *  comes back as `null` so the screen says "this server named nobody for that id" rather than printing
 *  the uuid or the display name of somebody else.
 */
export async function readTraveller(travelerId: string): Promise<TravellerRead | null> {
  const response = await fetch(`/api/v1/travelers/${encodeURIComponent(travelerId)}`, {
    headers: { "Content-Type": "application/json" },
  });
  if (response.status === 404) return null;
  if (!response.ok) throw await rejectionWithFacts(response);
  const body = (await response.json()) as Partial<TravellerRead>;
  return { id: String(body.id ?? travelerId), display_name: body.display_name ?? null };
}
