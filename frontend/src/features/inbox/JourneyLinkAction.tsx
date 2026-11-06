import { useState } from "react";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { accessCopyFor } from "../access/accessCopy";
import { formatWhen } from "../../lib/formatWhen";
import {
  copyJourneyLink,
  type JourneyLinkEnv,
  type JourneyLinkFailure,
  type JourneyLinkOutcome,
} from "../../lib/journeyLink";
import { claimFromRead, readShareGrants, type CopyClaim } from "./shareGrantRead";

type Props = {
  journeyId: string;
  /** `quiet` puts the control in a text row (Today), `primary` keeps the button the work bench
      already shipped.  The label is the same either way -- ORD-0366 asks for one more reachable
      entry, not for a second name to learn. */
  variant?: "primary" | "quiet";
};

const FAILURE_COPY: Record<JourneyLinkFailure, string> = {
  NO_JOURNEY: "inbox.journeyLinkNoJourney",
  INSECURE_CONTEXT: "inbox.journeyLinkInsecureContext",
  CLIPBOARD_UNAVAILABLE: "inbox.journeyLinkClipboardUnavailable",
};

/* The three sentences are looked up one at a time rather than off `ok`, because R2's whole point is that
   they are three different facts: "this device refused the write" may not borrow "there is no journey". */
function failureCopy(failure: JourneyLinkFailure): string {
  return FAILURE_COPY[failure];
}

/**
 * ORD-0447.  Once the clipboard write succeeded, the sentence in this slot is decided by the grant rows
 * the server listed, not by this device: the tier and the instants come from that answer, and every
 * branch that cannot read a state says it cannot, instead of claiming one.  That is what retires the
 * unconditional "whoever holds the link can open this journey" -- a link is still only this journey's
 * address, and whether a grant stands behind it is now read, not assumed.
 *
 * The tier and consequence words come from `accessCopy`, the table ORD-0436 put the sharing vocabulary
 * in.  They are not looked up through `t`, which silently hands back the key when a table does not hold
 * it -- that is how `access.visLinkRead` reached a traveller's screen the first time this ran.
 */
function claimCopy(
  claim: CopyClaim,
  locale: string,
  t: (key: string, params?: Record<string, string>) => string,
): string {
  const instant = (raw: string | null): string =>
    raw === null ? t("inbox.journeyLinkInstantUnreadable") : formatWhen(raw, { zh: locale.startsWith("zh"), pending: t("inbox.journeyLinkInstantUnreadable") });
  switch (claim.kind) {
    case "OPEN":
      return claim.expiresAt === null
        ? t("inbox.journeyLinkCopiedOpenNoEnd", { tier: accessCopyFor(locale, claim.labelKey) })
        : t("inbox.journeyLinkCopiedOpenUntil", {
            tier: accessCopyFor(locale, claim.labelKey),
            at: instant(claim.expiresAt),
          });
    case "TIER_CLOSED":
      return t("inbox.journeyLinkCopiedTierClosed", {
        tier: accessCopyFor(locale, claim.labelKey),
        consequence: accessCopyFor(locale, claim.consequenceKey),
      });
    case "REVOKED":
      return t("inbox.journeyLinkCopiedRevoked", { at: instant(claim.revokedAt) });
    case "NO_GRANT":
      return t("inbox.journeyLinkCopiedNoGrant");
    case "NOT_ANSWERED":
      return t("inbox.journeyLinkCopiedGrantUnanswered");
    case "UNKNOWN_TIER":
      return t("inbox.journeyLinkCopiedUnknownTier");
    default:
      return t("inbox.journeyLinkCopiedGrantReadFailed");
  }
}

/**
 * ORD-0357 — puts the current journey's own link within reach of a thumb.
 *
 * The link itself is not new (`/v8/<uuid>` has been the route since ORD-0351); what is new is that a
 * traveller can produce it without finding the address bar, which on a phone is hidden until you
 * scroll for it.  One row, one sentence of feedback: a refusal must not grow a second failure screen,
 * so the message replaces itself in the same slot rather than taking the panel over.
 */
export function JourneyLinkAction({ journeyId, variant = "primary" }: Props) {
  const t = useT();
  const { locale } = useLocale();
  const [outcome, setOutcome] = useState<JourneyLinkOutcome | null>(null);
  const [claim, setClaim] = useState<CopyClaim | null>(null);
  const [busy, setBusy] = useState(false);

  const copy = async () => {
    setBusy(true);
    const env: JourneyLinkEnv = {
      journeyId,
      origin: window.location.origin,
      clipboardAvailable: Boolean(navigator.clipboard?.writeText),
      isSecureContext: window.isSecureContext === true,
    };
    /* The grant is asked and the link written in one tap, and the read is allowed to fail without
       taking the copy with it: "this device refused the write" and "this screen could not read the
       grant" are two facts, and losing the second must not be reported as the first. */
    const [written, grants] = await Promise.all([
      copyJourneyLink(env, (text) => navigator.clipboard.writeText(text)),
      readShareGrants(journeyId).catch(() => ({ state: "READ_FAILED" }) as const),
    ]);
    setOutcome(written);
    setClaim(claimFromRead(grants as Parameters<typeof claimFromRead>[0]));
    setBusy(false);
  };

  const message =
    outcome === null
      ? null
      : outcome.failure !== null
        ? t(failureCopy(outcome.failure))
        /* Two sentences in one slot: what this device did, then what the server said. Chinese puts no
           space after a full stop, so the joiner follows the locale instead of being hardcoded. */
        : `${t("inbox.journeyLinkCopied")}${locale.startsWith("zh") ? "" : " "}${claimCopy(
            claim ?? { kind: "READ_FAILED" },
            locale,
            t,
          )}`;
  const copied = outcome?.ok === true;

  return (
    <div className="journey-link-action">
      <button
        type="button"
        className={variant === "quiet" ? "v8-quiet-button" : "v8-primary-button"}
        data-testid="journey-link-copy"
        data-cnx-field="journey_link_url"
        disabled={busy}
        onClick={() => void copy()}
      >
        {t("inbox.copyJourneyLink")}
      </button>
      {message !== null && (
        <p
          className="journey-link-message"
          data-testid="journey-link-message"
          data-cnx-field={copied ? "journey_link_url" : "journey_link_state"}
        >
          {message}
        </p>
      )}
    </div>
  );
}
