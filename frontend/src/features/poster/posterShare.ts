/**
 * ORD-0528 — "share this picture" without opening a second door.
 *
 * The poster is a rendering of a journey, so sharing it means handing somebody a link to the same
 * journey with the poster form asked for on the other end. That is exactly what the share family on
 * the line already does: `POST /api/v1/journeys/{id}/share-grants` mints the grant and its token, and
 * `GET /api/v1/journeys/{id}/share-grants` lists what the server believes exists. No new write
 * endpoint is added here, and no second token store.
 *
 * The one thing this file refuses to do is report success from the create answer. A 201 is this
 * device's claim about a write; the fact the traveller reads on screen is that the *list* door, read
 * back after the write, carries the row. If the list does not answer, or answers without the row, the
 * outcome is a named non-confirmation and the sheet says so. The row identity printed on screen
 * (`grant_id`) is read out of that list answer, not out of the create answer.
 *
 * ORD-0541 adds the other half of the same rule: a press that would write a second grant where one
 * already stands is a second answer to a question nobody re-asked. So this file asks first and writes
 * only when the server's own list offers nothing to hand over.
 */
import { createShareGrant } from "../access/accessDoors";
import { readShareGrants, type GrantRow } from "../inbox/shareGrantRead";
import { refusalReasonCode, RejectionWithFacts } from "../../lib/rejectionFacts";
import { VISIBILITY_LINK_READ } from "../access/accessVocabulary";

/** The address segment that asks for the poster form of a shared trip. */
export const POSTER_SHARE_FORM = "poster";

/** The tier the poster link is opened at: the same one the trip share uses for a copyable link.  The
 *  word is the ladder's own constant, not a string written again here, because the sentence below
 *  compares server rows against it and a typo would read as "nothing to reuse" forever. */
export const POSTER_SHARE_VISIBILITY = VISIBILITY_LINK_READ;

export function posterSharePath(token: string): string {
  return `/v8/shared/${encodeURIComponent(token)}/${POSTER_SHARE_FORM}`;
}

/** A standing link the traveller can be handed, taken off the server's list answer. */
export type ReusablePosterLink = {
  grant_id: string;
  token: string;
  rows_read: number;
};

export type PosterShareOutcome =
  | {
      state: "CONFIRMED";
      grant_id: string;
      path: string;
      rows_read: number;
      /** Which of the two ways here the outlet just took: `true` is "the server already had this link
       *  and nothing was written", `false` is "a grant was minted for this press". They are different
       *  facts, so they get different sentences -- one confirmation word for both would make the
       *  traveller's screen unable to tell ORD-0541's two cases apart. */
      reused: boolean;
    }
  | {
      state: "NOT_CONFIRMED";
      reason:
        | "CREATE_REFUSED"
        | "NO_TOKEN"
        | "NOT_IN_SERVER_READ"
        | "REVOKED_IN_SERVER_READ"
        | "READ_NOT_ANSWERED";
      http_status: number | string;
      detail: string;
    };

/**
 * The reuse predicate, kept pure so the counter-arm can judge it without a browser.
 *
 * A row is a link this outlet may hand over when three things are true of it, each read off a field
 * the list door actually answers with:
 *
 *   - `revoked_at` is `null`. A withdrawn grant is not a standing one, and the door keeps revoked
 *     rows in the answer on purpose (`backend/app/main.py` -- "a silently filtered row is
 *     indistinguishable from one that never existed"), so skipping them is this file's job.
 *   - `visibility` is exactly `LINK_READ`. The commander's ruling for ORD-0541, verbatim:
 *     「`visibility` 恰为 `LINK_READ` 才算可复用链接」. A `PUBLIC` row reaches a wider audience than
 *     this outlet ever asked for and a `PRIVATE` one opens nothing at all, so neither is *this*
 *     screen's link, whatever else they may be to the sharing screen.
 *   - `token` is a non-empty string. `ShareGrant.token` (`backend/app/schemas.py`) says list
 *     answers carry it precisely so the owner can build the link, so a row without one would be an
 *     id the traveller cannot do anything with; the guard is a `typeof` check rather than trust,
 *     because handing out `undefined` inside a share link is worse than writing a new grant.
 *
 * `expires_at` is deliberately not read here. An expiry instant compared with this device's clock is
 * the claim ORD-0447 took away from this screen: the doors that answer `SHARE_GRANT_EXPIRED` own
 * whether a token still opens, and a client that decided first would be making the same claim twice,
 * worse.
 *
 * When several rows qualify, the first one the door listed wins. That is not a preference invented
 * here: `list_share_grants` orders by `created_at, id` (`backend/app/main.py`), so this is the
 * oldest standing link-readable row, and the same answer every press -- which is what makes the
 * outlet converge instead of accumulating.
 */
export function reusablePosterLink(rows: GrantRow[]): ReusablePosterLink | null {
  for (const row of rows) {
    if (row.revoked_at !== null) continue;
    if (row.visibility !== POSTER_SHARE_VISIBILITY) continue;
    const token = typeof row.token === "string" ? row.token : "";
    if (token === "") continue;
    return { grant_id: row.id, token, rows_read: rows.length };
  }
  return null;
}

/** The list door, with its refusal turned into an outcome instead of an exception.
 *
 *  Both legs of `sharePosterLink` ask this and both need it: an exception escaping the pre-write leg
 *  would mean a press that wrote nothing and said nothing, and one escaping the read-back is the
 *  hazard ORD-0528's own X1 arm caught -- the sheet stuck on "正在向服务器登记…" with the button
 *  disabled forever. */
async function askServerWhatStands(journeyId: string): Promise<
  | { state: "ANSWERED"; rows: GrantRow[]; http_status: number }
  | { state: "REFUSED"; http_status: number | string; detail: string }
> {
  try {
    const read = await readShareGrants(journeyId);
    if (read.state !== "ANSWERED") {
      return { state: "REFUSED", http_status: read.http_status, detail: "" };
    }
    return { state: "ANSWERED", rows: read.rows, http_status: read.http_status };
  } catch (error) {
    return {
      state: "REFUSED",
      http_status: error instanceof RejectionWithFacts ? error.http_status : "unreachable",
      detail: refusalReasonCode(error as never) ?? "UNNAMED_REFUSAL",
    };
  }
}

/**
 * Hand the traveller a link: reuse the standing one the server already has, or write one and confirm
 * the write by reading it back.
 *
 * `travelerId` is the trip's own traveller, read off the payload the screen was given -- the door
 * takes this as a self-reported signature, so sending anything else would be inventing an identity.
 */
export async function sharePosterLink(
  journeyId: string,
  travelerId: string,
): Promise<PosterShareOutcome> {
  /* ORD-0541: ask before writing. A read that does not answer is a non-confirmation *and* a reason
     not to write, because the alternative is a device that mints a fresh grant every time the list
     door happens to be unhappy -- which is the accumulation this order exists to stop. */
  const before = await askServerWhatStands(journeyId);
  if (before.state !== "ANSWERED") {
    return {
      state: "NOT_CONFIRMED",
      reason: "READ_NOT_ANSWERED",
      http_status: before.http_status,
      detail: before.detail,
    };
  }
  const standing = reusablePosterLink(before.rows);
  if (standing !== null) {
    return {
      state: "CONFIRMED",
      grant_id: standing.grant_id,
      path: posterSharePath(standing.token),
      rows_read: standing.rows_read,
      reused: true,
    };
  }

  let created;
  try {
    created = await createShareGrant(journeyId, {
      visibility: POSTER_SHARE_VISIBILITY,
      created_by: travelerId,
      expires_at: null,
    });
  } catch (error) {
    /* Read the number off the object the door actually throws. `RejectionWithFacts` carries
       `http_status`; a `.status` probe here would answer `undefined` for every real refusal and the
       screen would print "状态码 unreachable" about a server that did answer with a number. That is
       what the first run of this arm caught: the stubbed 403 read back as unreachable. */
    const refused = error instanceof RejectionWithFacts ? error.http_status : "unreachable";
    return {
      state: "NOT_CONFIRMED",
      reason: "CREATE_REFUSED",
      http_status: refused,
      detail: refusalReasonCode(error as never) ?? "UNNAMED_REFUSAL",
    };
  }

  if (!created.token) {
    /* The grant may well exist -- the create door just did not hand back what opens it. That is a
       non-confirmation for this screen, because the thing it is about to claim is "here is a link",
       and a link with no minted token in it is a sentence about a door nobody can walk through. */
    return {
      state: "NOT_CONFIRMED",
      reason: "NO_TOKEN",
      /* Not `201`: the door that made this call returns the parsed body and drops the status, so the
         only honest value here is that no number was carried up to this layer. Writing 201 would be
         the screen quoting a response it never measured. */
      http_status: "not_carried_by_door",
      detail: created.id,
    };
  }

  const after = await askServerWhatStands(journeyId);
  if (after.state !== "ANSWERED") {
    return {
      state: "NOT_CONFIRMED",
      reason: "READ_NOT_ANSWERED",
      http_status: after.http_status,
      detail: after.detail,
    };
  }
  const row = after.rows.find((candidate) => candidate.id === created.id);
  if (!row) {
    return {
      state: "NOT_CONFIRMED",
      reason: "NOT_IN_SERVER_READ",
      http_status: after.http_status,
      detail: created.id,
    };
  }
  if (row.revoked_at) {
    return {
      state: "NOT_CONFIRMED",
      reason: "REVOKED_IN_SERVER_READ",
      http_status: after.http_status,
      detail: row.id,
    };
  }

  return {
    state: "CONFIRMED",
    grant_id: row.id,
    /* The token here is still the one the create answer minted: ORD-0541 reuses a row it read before
       writing, and this row was written by this press, so its token has only ever been carried by
       the create door. `shareGrantRead.ts` now types `token` on its rows, but taking the string from
       the row this device did not ask for would be a second source for one fact. */
    path: posterSharePath(created.token),
    rows_read: after.rows.length,
    reused: false,
  };
}
