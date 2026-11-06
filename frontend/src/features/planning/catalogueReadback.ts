/**
 * ORD-0504 · the one door this screen is allowed to speak a place count from.
 *
 * Before this file the "this city has no places" sentence was produced by the device: the run
 * fetched `/api/v1/places`, counted the ids it liked, and wrote a status string out of that
 * arithmetic (`aiGenerationRun.ts` still carries the judgement, and the wording it drives never
 * looked back at the store).  A traveller who then imported places read the same sentence as
 * before, because nothing re-asked the server - the screen was remembering, not reporting.
 *
 * The rule here is the smallest honest one:
 *  - the number only ever comes from rows this server returned, in this read, on this open;
 *  - a door that did not answer produces `unreadable`, which the sentence says out loud instead
 *    of falling back to a remembered count or a zero;
 *  - no identifier, no city code, no key name goes into the traveller's sentence - that is the
 *    evidence panel's job under 技术详情, not the copy's.
 */

export type CatalogueReadback = {
  /** `server` only when the door answered with a readable list; `unreadable` is a real state. */
  source: "server" | "unreadable";
  /** Rows the server returned.  `null` means we do not know, and is never displayed as 0. */
  placeCount: number | null;
  /** The status we actually saw, so an unreadable reading can name itself. */
  httpStatus: number | null;
};

export const UNREADABLE_SENTENCE_ZH = "这座城有多少地点，我们刚才没能从服务器读到。";
export const UNREADABLE_SENTENCE_EN =
  "We could not read how many places this city has from the server just now.";

export async function readCityCatalogue(
  cityCode: string | undefined,
  signal?: AbortSignal,
): Promise<CatalogueReadback> {
  if (!cityCode) {
    // No city named on this mount is not an empty catalogue; saying 0 here would be inventing a
    // fact the caller never had.
    return { source: "unreadable", placeCount: null, httpStatus: null };
  }
  try {
    const response = await fetch(
      `/api/v1/places?city_code=${encodeURIComponent(cityCode)}`,
      { headers: { Accept: "application/json" }, signal },
    );
    if (!response.ok) {
      return { source: "unreadable", placeCount: null, httpStatus: response.status };
    }
    const rows: unknown = await response.json();
    if (!Array.isArray(rows)) {
      return { source: "unreadable", placeCount: null, httpStatus: response.status };
    }
    return { source: "server", placeCount: rows.length, httpStatus: response.status };
  } catch {
    // A cancelled or broken read is reported as unreadable; it never becomes "no places".
    return { source: "unreadable", placeCount: null, httpStatus: null };
  }
}

/**
 * The traveller-facing sentence, built only from `readback`.  `null` and `0` are different facts
 * and get different sentences; that distinction is the whole reason this function exists.
 */
export function catalogueCountSentence(
  readback: CatalogueReadback | null,
  zh: boolean,
): string {
  if (!readback || readback.source !== "server" || readback.placeCount === null) {
    return zh ? UNREADABLE_SENTENCE_ZH : UNREADABLE_SENTENCE_EN;
  }
  const count = readback.placeCount;
  if (count === 0) {
    return zh
      ? "服务器回读：这座城我们现在一个地点都还没有。"
      : "Read back from the server: we hold no places for this city yet.";
  }
  return zh
    ? `服务器回读：这座城我们现在有 ${count} 个地点，可以排进行程。`
    : `Read back from the server: this city now has ${count} places that can go into a trip.`;
}
