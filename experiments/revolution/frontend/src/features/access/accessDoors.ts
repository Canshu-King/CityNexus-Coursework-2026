/**
 * ORD-0436 -- the doors this screen reads, in one file, so what it claims and what it asked the
 * server are separable.
 *
 * Two of them are on the line today and are named by their server-side position:
 *  - `GET /api/v1/journeys` -- `backend/app/main.py:2955`, the list door ORD-0383 slice 1 kept.
 *  - `GET /api/v1/journeys/{id}/agent-feed` -- `backend/app/main.py:7407`, the activity stream that
 *    reads `audit_logs` for one trip.
 * The sharing state belongs to ORD-0434 and is asked as three doors (`SHARING_DOOR_SUFFIXES` below).
 * None of them answers at this tip. A missing answer and an empty answer are different facts, so the
 * screen reports the doors it asked and their status rather than showing an empty roster, which would
 * read as "nobody shares this trip" -- a claim this tree has no source for.
 */
import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";

export const JOURNEY_LIST_PATH = "/journeys";
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

export function readJourneys(): Promise<JourneySummary[]> {
  return request<JourneySummary[]>(JOURNEY_LIST_PATH);
}

/** The server's `summary` is deliberately not trusted for the words on screen: for an action the
 *  projection does not know it derives a capitalised English phrase out of the token
 *  (`backend/app/main.py:7390`), which is a machine name wearing a sentence. The screen keeps the
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
