/* ORD-0444 -- the data half of the 收藏 container: the three doors, and nothing else.
 *
 * The screen this module serves is deliberately the smallest thing that can prove the container is
 * readable, so this file carries no refusal-code vocabulary, no measured-facts row and no
 * `addPlaceToCollection` helper that nothing calls.  The add door is in the contract
 * (`POST /api/v1/collections/{folder_id}/items`, contracts/openapi.yaml) and the generated types
 * cover it; a hand-written caller for it would be a promise this round does not make.
 *
 * Every path is written with the `/api/v1` prefix the runtime actually serves, because the
 * contract's own paths omit it (`contracts/openapi.yaml` -- `/collections`), and the parity door
 * (`tools/verify_openapi_parity.py`) keeps them apart on purpose.
 */

import { COLLECTION_COPY, COLLECTION_REFUSAL, type CollectionCopyKey } from "./collectionCopy";

export const COLLECTIONS_LIST_PATH = "/api/v1/collections";

/** The shelf answer, read off the generated contract type rather than re-declared here. */
export type CollectionShelf = {
  id: string;
  name: string;
  owner_traveler_id: string;
  created_at: string;
  updated_at: string;
  item_count: number;
  items: Array<{
    id: string;
    place_id: string;
    name: string;
    category: string;
    city_code: string;
    note: string | null;
    added_at: string;
  }>;
};

export type ReadOutcome =
  /* One of four named states, and the only thing the screen is allowed to say about a failure is
     which state it is in: the server's own words stay out of the DOM. */
  | { kind: "loaded"; shelves: CollectionShelf[] }
  | { kind: "unreachable" }
  | { kind: "refused" };

async function request<T>(path: string, init?: RequestInit): Promise<T | null> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) return null;
  const text = await response.text();
  return (text ? JSON.parse(text) : null) as T | null;
}

/**
 * The shelves this traveller owns.  `traveler_id` is required by the door (the contract says so at
 * `/collections.get.parameters[0]`), so an absent traveller is not asked about -- the screen names
 * that state itself instead of sending a call it knows the door will refuse.
 */
export async function readCollections(travelerId: string | null): Promise<ReadOutcome> {
  if (!travelerId) return { kind: "unreachable" };
  try {
    const shelves = await request<CollectionShelf[]>(
      `${COLLECTIONS_LIST_PATH}?traveler_id=${encodeURIComponent(travelerId)}`,
    );
    if (shelves === null) return { kind: "refused" };
    return { kind: "loaded", shelves };
  } catch {
    return { kind: "unreachable" };
  }
}

/** ORD-0510: the write leg.  Six named answers, because the door has six and a screen that folds
 *  "the shelf is gone" together with "the network is down" tells the traveller one thing while the
 *  server said another.  The reason code is read out of the envelope to split the two 404s --
 *  `backend/app/main.py` `add_collection_item` writes `COLLECTION_FOLDER_NOT_FOUND` / `PLACE_NOT_FOUND`
 *  into `error.reason_code`, and the status alone cannot tell them apart.  Nothing here carries the
 *  server's message text into the DOM; the caller picks a sentence from `collectionCopy.ts`. */
export type AddOutcome =
  | { kind: "added"; shelfId: string; itemId: string }
  | { kind: "already_there" }
  | { kind: "no_folder" }
  | { kind: "no_place" }
  | { kind: "refused" }
  | { kind: "unreachable" };

export function collectionItemsPath(folderId: string): string {
  return `${COLLECTIONS_LIST_PATH}/${encodeURIComponent(folderId)}/items`;
}

export async function addPlaceToCollection(
  folderId: string,
  placeId: string,
): Promise<AddOutcome> {
  if (!folderId || !placeId) return { kind: "refused" };
  try {
    const response = await fetch(collectionItemsPath(folderId), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ place_id: placeId }),
    });
    if (response.ok) {
      const text = await response.text();
      const row = text ? JSON.parse(text) : null;
      if (!row || typeof row.id !== "string") return { kind: "refused" };
      return { kind: "added", shelfId: folderId, itemId: row.id };
    }
    const text = await response.text();
    let reason = "";
    try {
      const body = text ? JSON.parse(text) : null;
      reason = String(body?.error?.reason_code ?? "");
    } catch {
      reason = "";
    }
    if (response.status === 404) {
      if (reason === "PLACE_NOT_FOUND") return { kind: "no_place" };
      return { kind: "no_folder" };
    }
    if (response.status === 409) return { kind: "already_there" };
    return { kind: "refused" };
  } catch {
    return { kind: "unreachable" };
  }
}

/** Which of this traveller's shelves actually hold the place, taken from a fresh read of the list
 *  door rather than from what the tap asked for.  ORD-0476 set the shape: a stop never shows 已打卡
 *  because a button was pressed, only because the read returned that row -- so a place never shows
 *  已收在 because a write looked like it worked. */
export function shelvesHolding(
  outcome: ReadOutcome,
  placeId: string,
): Array<{ id: string; name: string }> {
  if (outcome.kind !== "loaded") return [];
  return outcome.shelves
    .filter((shelf) => shelf.items.some((item) => item.place_id === placeId))
    .map((shelf) => ({ id: shelf.id, name: shelf.name }));
}

export function copyFor(key: CollectionCopyKey, zh: boolean): string {
  return COLLECTION_COPY[key][zh ? "zh" : "en"];
}
export function refusalCopy(zh: boolean): string {
  return COLLECTION_REFUSAL[zh ? "zh" : "en"];
}
