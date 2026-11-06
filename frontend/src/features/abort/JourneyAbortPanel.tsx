import { useState } from "react";

import { useLocale } from "../../i18n/LocaleContext";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import type { RawFacts } from "../../lib/rawFacts";
import { formatV8Date } from "../../lib/v8Formatting";
import type { V8Data } from "../../app/v8Types";
import {
  abortJourney,
  refusalDetails,
  refusalReasonCode,
  RejectionWithFacts,
  type AbortReadbackJourney,
} from "./journeyAbort";
import {
  ABORT_REFUSAL_COPY,
  abortCopy,
  fillAbort,
  type JourneyAbortCopyKey,
} from "./journeyAbortCopy";
import "./journeyAbort.css";

/**
 * ORD-0520 中止 -- the trip's own abort control, and the notice 实时行程 shows instead of a countdown.
 *
 * Mounted through the panel registry (`app/v8PanelRegistry.tsx`: a `trips-abort` entry on the
 * trips-tab surface, and the guard inside the `live` route entry), so this file plus that table is
 * the whole front-end change and the shell stays shut -- the way ORD-0402 slice 2 and ORD-0472 do it.
 *
 * Four things this screen may not do:
 *  - It may not decide liveness.  The row it renders carries `live_view` from the server and the only
 *    reading here is `included` / `reason_code`; a screen that filtered `status` itself would hold a
 *    second copy of the rule, so when the field is missing this screen says nothing about liveness at
 *    all rather than guessing (the live route keeps its own content for a payload without a decision).
 *  - It may not promise a way back.  This server has no un-abort door, so the consequence is said
 *    before the press and no second action appears after it.  Getting a *dropped* trip back is the
 *    recycle bin's verb (找回), and 中止 dropped nothing, so it is not offered here.
 *  - It may not answer a refusal with a code.  Every `error.reason_code` this door can name has its
 *    own sentence; a name this file does not know is said as unnamed, and the token plus this
 *    device's own reading go on the 技术详情 face.
 *  - It may not report a write it did not read.  After the POST the facts shown come from the body
 *    the server built off the row, and the shell re-reads through `onAborted`; a missing read-back is
 *    named, not filled in.
 */

type AbortedOutcome = { kind: "aborted"; journey: AbortReadbackJourney };
type RefusalOutcome = {
  kind: "refusal";
  reasonCode: string | null;
  copyKey: JourneyAbortCopyKey | null;
  facts: RawFacts | null;
  envelope: unknown;
  when: string | null;
  reason: string | null;
};
type EmptyOutcome = { kind: "empty" };
type TransportOutcome = { kind: "transport" };
type Outcome = AbortedOutcome | RefusalOutcome | EmptyOutcome | TransportOutcome;

function sayFor(locale: string, key: JourneyAbortCopyKey, params: Record<string, string | number> = {}) {
  return fillAbort(abortCopy(locale, key), params);
}

/** The stored facts, or the sentence that names their absence -- never a blank, never a guess. */
function StoredFacts({
  journey,
  locale,
  testId,
}: {
  journey: { abort_reason?: string | null; aborted_at?: string | null };
  locale: string;
  testId: string;
}) {
  const reason = journey.abort_reason ? String(journey.abort_reason) : null;
  const at = journey.aborted_at ? String(journey.aborted_at) : null;
  return (
    <div className="cn-abort__facts" data-testid={testId}>
      {reason ? (
        <p className="cn-abort__reason" data-testid={`${testId}-reason`}>
          {sayFor(locale, "abort.storedReason", { reason })}
        </p>
      ) : (
        <p className="cn-abort__missing" data-testid={`${testId}-reason-missing`}>
          {sayFor(locale, "abort.readbackMissing")}
        </p>
      )}
      {at ? (
        <p className="cn-abort__when" data-testid={`${testId}-at`}>
          {sayFor(locale, "abort.storedAt", { when: formatV8Date(at, locale) })}
        </p>
      ) : (
        <p className="cn-abort__missing" data-testid={`${testId}-at-missing`}>
          {sayFor(locale, "abort.readbackMissing")}
        </p>
      )}
    </div>
  );
}

export function JourneyAbortPanel({
  data,
  onAborted,
}: {
  data: V8Data;
  onAborted: () => void;
}) {
  const { locale } = useLocale();
  const say = (key: JourneyAbortCopyKey, params: Record<string, string | number> = {}) =>
    sayFor(locale, key, params);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const journey = data.journey;
  const live = journey.live_view;
  const aborted = live ? live.included === false : false;

  async function submit() {
    if (!reason.trim()) {
      // The device refuses its own empty box before asking, so a tap cannot store a trip that ended
      // "for no reason" -- and the server's 422 stays the authority if this check is ever bypassed.
      setOutcome({ kind: "empty" });
      return;
    }
    setBusy(true);
    setOutcome(null);
    try {
      const answer = await abortJourney(journey.id, journey.traveler_id, reason.trim());
      setOutcome({ kind: "aborted", journey: answer });
      onAborted();
    } catch (cause) {
      const reasonCode = refusalReasonCode(cause);
      const details = refusalDetails(cause) ?? {};
      setOutcome({
        kind: "refusal",
        reasonCode,
        copyKey: reasonCode ? (ABORT_REFUSAL_COPY[reasonCode] ?? null) : null,
        facts: cause instanceof RejectionWithFacts ? cause.facts : null,
        envelope: cause instanceof RejectionWithFacts ? cause.envelope : null,
        when: details.aborted_at ? String(details.aborted_at) : null,
        reason: details.abort_reason ? String(details.abort_reason) : null,
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="cn-abort" data-testid="journey-abort" data-abort-state={aborted ? "ABORTED" : "NOT_ABORTED"}>
      {/* Said before the press, because "what happens, and can I come back" is what a traveller needs
          in order to decide, and there is no un-abort door to rescue an impulsive tap. */}
      <p className="cn-abort__explains" data-testid="journey-abort-explains">
        {say("abort.explains")}
      </p>

      {aborted ? (
        <div className="cn-abort__state" data-testid="journey-abort-state">
          <p className="cn-abort__badge" data-testid="journey-abort-badge">
            {say("abort.stateOnScreen")}
          </p>
          <StoredFacts journey={journey} locale={locale} testId="journey-abort-stored" />
        </div>
      ) : (
        /* Kept one line high until it is opened: this entry sits above the itinerary so the action
           is reachable without scrolling past the whole plan, and a full always-open form there
           would push the trip itself off the first screen (the fold rule ORD-0401 measured). */
        <details className="cn-abort__form">
          <summary className="cn-abort__summary" data-testid="journey-abort-open">
            {say("abort.title")}
          </summary>
          <label className="cn-abort__label" htmlFor="cn-abort-reason">
            {say("abort.reasonLabel")}
          </label>
          <textarea
            id="cn-abort-reason"
            className="cn-abort__input"
            data-testid="journey-abort-reason"
            value={reason}
            maxLength={240}
            rows={2}
            placeholder={say("abort.reasonPlaceholder")}
            onChange={(event) => setReason(event.target.value)}
          />
          <button
            type="button"
            className="v8-quiet-button"
            data-testid="journey-abort-submit"
            data-contrast-role="control"
            disabled={busy}
            onClick={() => void submit()}
          >
            {busy ? say("abort.busy") : say("abort.action")}
          </button>
        </details>
      )}

      {outcome && outcome.kind === "aborted" ? (
        <div className="cn-abort__done" data-testid="journey-abort-done">
          <p>{say("abort.done", { title: journey.title })}</p>
          <StoredFacts journey={outcome.journey} locale={locale} testId="journey-abort-readback" />
        </div>
      ) : null}

      {outcome && outcome.kind !== "aborted" ? (
        <div className="cn-abort__refusal">
          {outcome.kind === "refusal" && outcome.copyKey ? (
            <p data-testid="journey-abort-refusal-copy">
              {say(outcome.copyKey, {
                ...(outcome.when ? { when: outcome.when } : {}),
                ...(outcome.reason ? { reason: outcome.reason } : {}),
              })}
            </p>
          ) : outcome.kind === "refusal" ? (
            <p data-testid="journey-abort-refusal-unnamed">{say("abort.unnamed")}</p>
          ) : outcome.kind === "empty" ? (
            <p data-testid="journey-abort-empty-reason">{say("abort.emptyReason")}</p>
          ) : (
            <p data-testid="journey-abort-transport">{say("abort.transport")}</p>
          )}
          {outcome.kind === "refusal" && outcome.reasonCode ? (
            <details className="cn-abort__details">
              <summary data-testid="journey-abort-details-toggle">{say("abort.details")}</summary>
              <span className="cnx-measured-fact" data-testid="journey-abort-reason-code">
                {outcome.reasonCode}
              </span>
              {outcome.facts ? (
                <CnxMeasuredFacts
                  facts={outcome.facts}
                  zh={locale.startsWith("zh")}
                  testId="journey-abort-refusal"
                  envelope={outcome.envelope}
                />
              ) : null}
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/**
 * What 实时行程 shows for a trip the server has decided is not live.  It replaces the live content
 * for that row rather than sitting under it: a countdown beside "this trip is aborted" is two
 * answers on one screen.  Only reached when the server's own `live_view.included` is false, so the
 * field is present by definition -- there is no client-side guess about a payload that lacks it, and
 * a device that inferred one would be deciding liveness, which is this server's call (ORD-0520).
 */
export function JourneyAbortedNotice({ data }: { data: V8Data }) {
  const { locale } = useLocale();
  const say = (key: JourneyAbortCopyKey, params: Record<string, string | number> = {}) =>
    sayFor(locale, key, params);
  const journey = data.journey;
  return (
    <section className="v8-page" data-testid="journey-aborted-notice" data-live-view="excluded">
      <p className="v8-kicker">{say("abort.stateOnScreen")}</p>
      <h1>{journey.title}</h1>
      <p className="v8-lede" data-testid="journey-aborted-why">
        {say("abort.liveExcluded")}
      </p>
      <StoredFacts journey={journey} locale={locale} testId="journey-aborted-facts" />
    </section>
  );
}
