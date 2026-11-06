import { useState } from "react";
import { useT } from "../../i18n/LocaleContext";
import {
  copyJourneyLink,
  type JourneyLinkEnv,
  type JourneyLinkFailure,
  type JourneyLinkOutcome,
} from "../../lib/journeyLink";

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
function outcomeCopy(outcome: JourneyLinkOutcome, t: (key: string) => string): string {
  const failure = outcome.failure;
  if (failure === null) return t("inbox.journeyLinkCopied");
  return t(FAILURE_COPY[failure]);
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
  const [outcome, setOutcome] = useState<JourneyLinkOutcome | null>(null);
  const [busy, setBusy] = useState(false);

  const copy = async () => {
    setBusy(true);
    const env: JourneyLinkEnv = {
      journeyId,
      origin: window.location.origin,
      clipboardAvailable: Boolean(navigator.clipboard?.writeText),
      isSecureContext: window.isSecureContext === true,
    };
    setOutcome(await copyJourneyLink(env, (text) => navigator.clipboard.writeText(text)));
    setBusy(false);
  };

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
      {outcome !== null && (
        <p
          className="journey-link-message"
          data-testid="journey-link-message"
          data-cnx-field={outcome.ok ? "journey_link_url" : "journey_link_state"}
        >
          {outcomeCopy(outcome, t)}
        </p>
      )}
    </div>
  );
}
