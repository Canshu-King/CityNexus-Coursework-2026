import type { components } from "../generated/api";

/**
 * ORD-0238 -- the traveller's stated interests get stored in the contract that already exists
 * (`PUT /api/v1/travelers/{id}/preferences`, `PreferenceInput = {category, key, value, weight}`),
 * typed against the generated OpenAPI types rather than a hand-copied shape, and keyed the way the
 * server's own fixture keys them (`data/mock/traveler_demo.json`: `{category:"interest",
 * key:"culture", value:true, weight:0.9}`).
 *
 * What this file deliberately does NOT do: invent a ranking, or rename her words to match the
 * server's vocabulary.  The wizard never asked how strongly she holds an interest, and its option
 * list only partly overlaps the fixture's (`food` vs `local_food`, `seaside` vs `waterfront`), so
 * either move would put a made-up fact into a column a planner may one day sort by.
 */

type PreferenceInput = components["schemas"]["PreferenceInput"];
type Preference = components["schemas"]["Preference"];

/** Stated, not ranked -- the midpoint of the server's own 0..1 range, and the only value we can say. */
export const STATED_NOT_RANKED_WEIGHT = 0.5;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return body as T;
}

export type InterestSync = {
  /** The rows the server now holds for this traveller, read back after the write. */
  rows: Preference[];
  /** Non-interest rows carried over untouched, because `PUT` replaces the whole list. */
  kept: number;
  /** Interest rows written from what the traveller clicked. */
  written: number;
};

export async function syncStatedInterests(
  travelerId: string,
  interests: string[],
): Promise<InterestSync> {
  const current = await request<Preference[]>(
    `/api/v1/travelers/${travelerId}/preferences`,
  );
  const rows = Array.isArray(current) ? current : [];
  /* `PUT` deletes every row of this traveller and re-adds the payload, so anything that is not an
     interest has to travel with it -- saving her interests must not quietly erase her walking
     limit or her pace row. */
  const others: PreferenceInput[] = rows
    .filter((row) => String(row?.category ?? "") !== "interest")
    .map((row) => ({
      category: row.category,
      key: row.key,
      value: row.value,
      weight: row.weight,
    }));
  const keys = [...new Set((interests ?? []).map((key) => String(key).trim()).filter(Boolean))];
  const fresh: PreferenceInput[] = keys.map((key) => ({
    category: "interest",
    key,
    value: true,
    weight: STATED_NOT_RANKED_WEIGHT,
  }));
  await request(`/api/v1/travelers/${travelerId}/preferences`, {
    method: "PUT",
    body: JSON.stringify([...others, ...fresh]),
  });
  const after = await request<Preference[]>(
    `/api/v1/travelers/${travelerId}/preferences`,
  );
  return {
    rows: Array.isArray(after) ? after : [],
    kept: others.length,
    written: fresh.length,
  };
}
