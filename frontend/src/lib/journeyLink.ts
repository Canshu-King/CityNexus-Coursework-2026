/**
 * ORD-0357 — the journey link a traveller can actually take out of the phone.
 *
 * ORD-0351 made `/v8/<journey uuid>` a real entry point, but the only place that URL is ever written
 * is `V8Experience.tsx`'s `history.pushState`, and on a phone the address bar is hidden -- so the
 * traveller could open a journey and still had no way to hand its link to anyone.
 *
 * The URL is built from `window.location.origin` rather than a constant: the same bundle answers on a
 * lane port, the phone tunnel and a filed domain, and a hard-coded base would be wrong on two of the
 * three.  It lives here, away from the component, because all three failure shapes have to be judged
 * without standing in front of a browser to revoke a permission.
 */

export type JourneyLinkFailure = "NO_JOURNEY" | "INSECURE_CONTEXT" | "CLIPBOARD_UNAVAILABLE";

/**
 * ORD-0404: one definition of "this address segment names a journey, not a screen".
 *
 * ORD-0351 made `/v8/<journey uuid>` a valid entry, and ORD-0404 needs to tell that apart from a
 * segment that names no screen at all: answering "this address has no screen" to a real journey
 * link would be a new lie built on top of the one being fixed. `main.tsx` used to carry its own
 * copy of this pattern; two copies drift, and only one of them is read when a link is judged.
 */
export const JOURNEY_ID_SEGMENT =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The first `/v8/` segment, exactly as written (a journey id is matched against a BINARY-collated
 * SQLite TEXT column, so case is not this function's to normalize). "" means bare `/v8/`.
 */
export function v8FirstSegment(pathname: string): string {
  return pathname.replace(/^\/v8\/+/, "").split("/")[0] ?? "";
}

/**
 * ORD-0465: the value after a token route's name -- `/v8/shared/<this part>` of a share link.
 *
 * Not lower-cased, for the same reason `v8FirstSegment` is not: the server looks this string up in a
 * BINARY-collated column (`JourneyShareGrant.token`, `backend/app/db/models.py`), and the tokens it
 * mints are `secrets.token_urlsafe(24)` -- mixed case by construction. A normaliser here would quietly
 * turn a valid link into `SHARE_GRANT_UNKNOWN`, and the screen would tell the traveller their link
 * does not exist.
 *
 * Anything non-empty is handed to the door, including shapes this file could not recognise as a token.
 * Judging a token is the server's job and it has a named answer for each of its three verdicts; a
 * client-side pattern would only be a second, weaker judge that turns "the link you gave me has no
 * token in it" into a guess about a token it never looked at. `""` is the one case this device can
 * decide on its own, because nothing was sent.
 */
export function v8SecondSegment(pathname: string): string {
  return pathname.replace(/^\/v8\/+/, "").split("/")[1] ?? "";
}

/**
 * ORD-0528: the value after a share token -- `/v8/shared/<token>/<this part>`.
 *
 * This segment chooses which *screen* a grant opens as; it is not part of the authorization. The token
 * above it stays the only thing the door sends, so a link cannot be given more reach by having its
 * tail edited, and a stranger's clipboard edits nothing either: a poster link and a trip link are the
 * same grant read twice.
 *
 * Read exactly as written and lower-cased by the caller, because a mistyped tail (`/Poster`, `/poster/`)
 * should fall back to the trip screen rather than dead-end a traveller who holds a valid token.
 */
export function v8ThirdSegment(pathname: string): string {
  return pathname.replace(/^\/v8\/+/, "").split("/")[2] ?? "";
}

export type JourneyLinkEnv = {
  journeyId: string;
  origin: string;
  clipboardAvailable: boolean;
  isSecureContext: boolean;
};

/* One shape, not a discriminated union: this tree is type-checked with `strict: false`
   (`tsconfig.check.json`), where narrowing on an `ok: true | false` discriminant does not hold. */
export type JourneyLinkOutcome = {
  ok: boolean;
  /** The link this attempt was about -- null only when there is no journey, so "the clipboard refused"
      keeps holding a real link and "no journey" never holds a pseudo one. */
  url: string | null;
  failure: JourneyLinkFailure | null;
};

/** Null, never a link with an empty tail: `/v8/` opens the hub, which is not the traveller's journey. */
export function buildJourneyLinkUrl(journeyId: string, origin: string): string | null {
  const id = String(journeyId ?? "").trim();
  if (id === "") return null;
  return new URL(`/v8/${id}`, origin).href;
}

/**
 * Ordered by what the traveller can act on, and deliberately not collapsed into one sentence: the
 * clipboard refusing is this device's permission, the journey missing is the product's state, and
 * saying one for the other is the defect R2 names.
 */
export function journeyLinkFailure(env: JourneyLinkEnv): JourneyLinkFailure | null {
  if (buildJourneyLinkUrl(env.journeyId, env.origin) === null) return "NO_JOURNEY";
  if (!env.isSecureContext) return "INSECURE_CONTEXT";
  if (!env.clipboardAvailable) return "CLIPBOARD_UNAVAILABLE";
  return null;
}

/** `write` is injected so "the browser rejected the write" is a leg that can be measured, not assumed. */
export async function copyJourneyLink(
  env: JourneyLinkEnv,
  write: (text: string) => Promise<void>,
): Promise<JourneyLinkOutcome> {
  const url = buildJourneyLinkUrl(env.journeyId, env.origin);
  if (url === null) return { ok: false, url: null, failure: "NO_JOURNEY" };
  if (!env.isSecureContext) return { ok: false, url, failure: "INSECURE_CONTEXT" };
  if (!env.clipboardAvailable) return { ok: false, url, failure: "CLIPBOARD_UNAVAILABLE" };
  try {
    await write(url);
  } catch {
    return { ok: false, url, failure: "CLIPBOARD_UNAVAILABLE" };
  }
  return { ok: true, url, failure: null };
}
