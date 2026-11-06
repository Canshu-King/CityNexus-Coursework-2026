import { parseJsonOrNull } from "../../lib/rawFacts";
import { RejectionWithFacts, rejectionWithFacts } from "../../lib/rejectionFacts";
import type { PendingCandidate, PendingPlaceRow } from "./pendingSet";

/**
 * ORD-0445 -- the two doors the 待规划 screen reads, and nothing else.
 *
 * Both are already on the line and already declared, so this order ships no new server surface:
 *  - `GET /api/v1/journeys/{journey_id}/provider-mesh` -- `backend/app/main.py`, the hand-built
 *    dict at `:1712` whose `real_place_candidates` list is the captured-for-this-trip half of the set.
 *  - `GET /api/v1/places?city_code=...` -- `backend/app/main.py`, read only to put a category
 *    word on a plan stop that lost its day.  It answers without the server-owned and structural rows
 *    by default (`:5373`-`:5374`), which is exactly why a missing category is named on screen instead
 *    of guessed.
 *
 * An answer that is not the list this screen needs is returned as `no_list`, not as an empty list.
 * That distinction is the whole point: 「没有待规划的地点」 and 「这台服务器没答出那份清单」 are
 * different facts, and ORD-0263 is the record of what happens when a screen reads one as the other.
 */

/** `no_list` = the door answered but did not carry the list this screen needs. It deliberately
 *  carries no measurement: this device only measured what a failed read gives it, and an unshaped
 *  200 is reported by its own top-level keys, which is the honest reading (`keys`). */
export type Door<T> = { kind: "list"; rows: T[] } | { kind: "no_list"; keys: string[] };

async function getAnswer(path: string): Promise<unknown> {
  const response = await fetch(`/api/v1${path}`, { headers: { Accept: "application/json" } });
  /* The refusal is measured before this device touches the body: `Response.text()` is single-use,
     and consuming it first would destroy the very bytes the details face has to count. */
  if (!response.ok) throw await rejectionWithFacts(response);
  return parseJsonOrNull(await response.text());
}

function topLevelKeys(json: unknown): string[] {
  if (typeof json !== "object" || json === null || Array.isArray(json)) return [];
  return Object.keys(json).sort();
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export async function readCapturedPlaces(journeyId: string): Promise<Door<PendingCandidate>> {
  const json = await getAnswer(`/journeys/${encodeURIComponent(journeyId)}/provider-mesh`);
  const raw = (json as { real_place_candidates?: unknown } | null)?.real_place_candidates;
  if (!Array.isArray(raw)) return { kind: "no_list", keys: topLevelKeys(json) };
  const rows = raw
    .map((item): PendingCandidate | null => {
      const record = item as Record<string, unknown>;
      const id = text(record?.id);
      const name = text(record?.name);
      /* A row this device cannot name is not a row it may render; it is still counted, so the
         denominator on the details face stays true. */
      if (!id || !name) return null;
      return {
        id,
        name,
        category: text(record.category),
        provider_key: text(record.provider_key),
        status: text(record.status),
        materialized_place_id: text(record.materialized_place_id),
        captured_at: text(record.captured_at),
      };
    })
    .filter((row): row is PendingCandidate => row !== null);
  return { kind: "list", rows };
}

export async function readCityPlaces(cityCode: string): Promise<Door<PendingPlaceRow>> {
  const json = await getAnswer(`/places?city_code=${encodeURIComponent(cityCode)}`);
  /* This door's own contract is a bare array (`response_model=list[PlaceSchema]`,
     `backend/app/main.py`), so anything else -- an envelope, a page object, no bytes at all --
     is `no_list` rather than an empty catalogue. */
  if (!Array.isArray(json)) return { kind: "no_list", keys: topLevelKeys(json) };
  const rows = json
    .map((item): PendingPlaceRow | null => {
      const record = item as Record<string, unknown>;
      const id = text(record?.id);
      if (!id) return null;
      return { id, category: text(record.category) };
    })
    .filter((row): row is PendingPlaceRow => row !== null);
  return { kind: "list", rows };
}

export { RejectionWithFacts };
