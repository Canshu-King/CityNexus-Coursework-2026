/**
 * ORD-0447 — what the server says about this journey's share grants, asked at the moment the traveller
 * copies the link.
 *
 * This file deliberately answers no question about reachability.  The doors that reply 409
 * `SHARE_GRANT_EXPIRED` / `SHARE_GRANT_REVOKED` own that; a screen that compares `expires_at` with the
 * device clock would be making the same claim twice, worse.  So the copy here may only say which
 * authorization rows exist, at which tier, and with which server-recorded instants — or say that the
 * server did not answer.
 *
 * The one door asked is ORD-0434's list door, named by its route rather than invented:
 * `GET /api/v1/journeys/{journey_id}/share-grants`.
 */
import { rejectionWithFacts } from "../../lib/rejectionFacts";
import type { AccessCopyKey } from "../access/accessCopy";
import {
  VISIBILITY_LINK_READ,
  VISIBILITY_LADDER,
  VISIBILITY_PUBLIC,
} from "../access/accessVocabulary";

export const GRANT_DOOR_SUFFIX = "/share-grants";

/** A row exactly as the door sends it. `expires_at` / `revoked_at` are `str | None` in the contract,
 *  and `null` here means the server recorded no such instant — not "no end", which is a different
 *  claim that only the create door's own words support. */
export type GrantRow = {
  id: string;
  visibility: string;
  expires_at: string | null;
  revoked_at: string | null;
  /**
   * ORD-0541: the door has always sent this (`ShareGrant.token`, `backend/app/schemas.py` -- "list
   * answers carry it so the owner can build or revoke the link"), and this file left it off the row
   * because nothing here needed it. Reusing an existing link instead of minting a second grant needs
   * it: a row without its token is an id the traveller cannot do anything with.
   *
   * Optional, and deliberately so. Nothing in this module's own reading depends on the field, and the
   * claim it exists to keep making -- "which authorization rows exist, at which tier, with which
   * server-recorded instants" -- says nothing about reachability and still says nothing now.
   */
  token?: string;
};

export type GrantRead =
  | { state: "ANSWERED"; http_status: number; rows: GrantRow[] }
  | { state: "NOT_ANSWERED"; http_status: number };

export type CopyClaim =
  /** An unrevoked, link-readable grant. The tier sentence comes from the shared ladder. */
  | { kind: "OPEN"; labelKey: AccessCopyKey; consequenceKey: AccessCopyKey; expiresAt: string | null }
  /** A grant whose tier is not one this tree has a ladder row for: claims nothing about reach. */
  | { kind: "UNKNOWN_TIER" }
  /** The unrevoked grants are all tiers a link does not open. */
  | { kind: "TIER_CLOSED"; labelKey: AccessCopyKey; consequenceKey: AccessCopyKey }
  /** Every grant row the server listed carries a revocation instant. */
  | { kind: "REVOKED"; revokedAt: string | null }
  | { kind: "NO_GRANT" }
  | { kind: "NOT_ANSWERED"; httpStatus: number }
  | { kind: "READ_FAILED" };

/** 404/405/501 mean "this server has no such door", which is a different fact from an empty roster —
 *  and both differ from a read that failed. Same tri-state `accessDoors.ts` settled on. */
const ABSENT_STATUSES = new Set([404, 405, 501]);

export async function readShareGrants(
  journeyId: string,
  ask: typeof fetch = fetch,
): Promise<GrantRead> {
  /* An authorization read must not be answerable from a cache: the whole sentence depends on whether
     a revocation has landed.  Measured, not assumed -- `screen_arms_r4.log` shows every page read
     answered by the network (`answered_by_service_worker=false`, no `cache-control`/`age`/`etag`), so
     no layer between this fetch and the door was serving stale rows; the first lag I saw was my own
     instrument reading the slot before React repainted it. */
  const response = await ask(`/api/v1/journeys/${encodeURIComponent(journeyId)}${GRANT_DOOR_SUFFIX}`, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
  });
  if (ABSENT_STATUSES.has(response.status)) return { state: "NOT_ANSWERED", http_status: response.status };
  if (!response.ok) throw await rejectionWithFacts(response);
  const rows = (await response.json()) as GrantRow[];
  return { state: "ANSWERED", http_status: response.status, rows: Array.isArray(rows) ? rows : [] };
}

function ladderRow(level: string) {
  return VISIBILITY_LADDER.find((row) => row.level === level) ?? null;
}

/** Widest-first tiers a bare link opens, taken from the ladder's own names. Typed as `string` because
 *  `visibility` arrives as whatever the server sent, and comparing it must not narrow it away. */
const LINK_READABLE: ReadonlySet<string> = new Set([VISIBILITY_LINK_READ, VISIBILITY_PUBLIC]);

/**
 * The only derivation, kept pure so the counter-arm is checkable without a browser.
 *
 * Order matters and each step reads one server field: a revocation instant beats an expiry instant,
 * an unrevoked link-readable row beats a closed tier, and nothing at all beats a guess.  An unknown
 * tier answers `UNKNOWN_TIER` rather than borrowing the nearest sentence.
 */
export function claimFromRead(read: GrantRead | { state: "READ_FAILED" }): CopyClaim {
  if (read.state === "READ_FAILED") return { kind: "READ_FAILED" };
  if (read.state === "NOT_ANSWERED") return { kind: "NOT_ANSWERED", httpStatus: read.http_status };
  const rows = read.rows;
  if (rows.length === 0) return { kind: "NO_GRANT" };

  const standing = rows.filter((row) => row.revoked_at === null);
  if (standing.length === 0) {
    return { kind: "REVOKED", revokedAt: rows[0]?.revoked_at ?? null };
  }

  const open = standing.find((row) => LINK_READABLE.has(row.visibility));
  if (open !== undefined) {
    const ladder = ladderRow(open.visibility);
    if (ladder === null) return { kind: "UNKNOWN_TIER" };
    return {
      kind: "OPEN",
      labelKey: ladder.labelKey,
      consequenceKey: ladder.consequenceKey,
      expiresAt: open.expires_at,
    };
  }

  const ladder = ladderRow(standing[0].visibility);
  if (ladder === null) return { kind: "UNKNOWN_TIER" };
  return { kind: "TIER_CLOSED", labelKey: ladder.labelKey, consequenceKey: ladder.consequenceKey };
}
