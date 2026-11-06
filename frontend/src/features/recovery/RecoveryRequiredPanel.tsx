/**
 * Mid-trip HARD recovery + stale-evidence panel.
 *
 * Surfaces the durable recovery state the backend writes when a newly
 * confirmed HARD capability (or an evidence regression) invalidates the
 * ACTIVE plan.  The active plan is never silently rewritten, so the traveller
 * is given an explicit, governed choice.
 *
 * ORD-0369 re-walked this card on a 390x844 viewport, because it is the first thing a
 * traveller hits when a chosen plan can no longer be verified -- and it was the one screen
 * on that chain that answered the traveller with the backend's own English prose.  Three
 * measured defects, all fixed here:
 *  - every sentence was a hardcoded English literal, so a Chinese device got an unreadable
 *    gate in front of the way out (zh-CN is now supplied, like everywhere else);
 *  - `setMessage(body.error.message)` put the upstream sentence on the traveller's line,
 *    where Q64/Q67 say only an attributed 技术详情 fold may quote it (and say which field);
 *  - `catch { "check your connection" }` blamed the network for any exception, including
 *    the 422 and 404 this card itself receives.  Naming the cause is
 *    `lib/failureAttribution`'s job and it now goes through it.
 * The measured refusals: `POST .../plans/generate` answers 422 on the sealed demo journey
 * ("no feasible plan candidates"), and `POST .../recovery/acknowledge` answers 404
 * `no open recovery to acknowledge` -- a stale card has no recovery record to acknowledge,
 * so that button can never succeed in exactly the state that shows it.
 */

import { useState } from "react";
import type { RecoveryState } from "../../app/v8Types";
import { RawFactsPanel } from "../../components/RawFactsPanel";
import { deviceReadout } from "../../lib/failureAttribution";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { classifyReadFailure, recordAttribution } from "../../lib/failureAttribution";
import { rejectionWithFacts } from "../../lib/rejectionFacts";
import type { RawFacts } from "../../lib/rawFacts";

type Props = {
  journeyId: string;
  recovery?: RecoveryState | null;
  flags?: {
    needs_review: boolean;
    recovery_required: boolean;
    staleness: boolean;
    feasibility_failures: string[];
  } | null;
  onRefresh: () => void;
};

/** What this screen tells the traveller, kept as a key so switching language re-renders the
 *  truth instead of freezing whichever language happened to be on screen when it failed. */
type Notice = {
  key: string;
  params: Record<string, string | number>;
  /** Whose words the quoted line is. `device` means this screen composed it and it must not
   *  borrow the 「服务器给出的原因」 label -- ORD-0280's exact defect. */
  origin: "server" | "device";
  /** The envelope field a quoted sentence came from, or null when nothing was quoted. */
  field: string | null;
  /** Server-authored text, mountable only inside the 技术详情 fold (Q67). */
  quote: string | null;
  facts: RawFacts | null;
};

async function post(path: string, body?: unknown): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** The refusal, reduced to what may be shown: this device's reading, the server's own name
 *  for the refusal, and the server's sentence held for the fold. Nothing else survives. */
async function noticeFromRefusal(key: string, response: Response): Promise<Notice> {
  const refusal = await rejectionWithFacts(response);
  const envelope = (refusal.envelope ?? {}) as {
    error?: { code?: unknown; message_key?: unknown; message?: unknown };
    detail?: unknown;
  };
  const nested = envelope.error ?? {};
  const code =
    (typeof nested.code === "string" && nested.code) ||
    (typeof nested.message_key === "string" && nested.message_key) ||
    deviceReadout({ http_status: refusal.http_status }, ["http_status"]);
  const sentence = typeof nested.message === "string" ? nested.message.trim() : "";
  const detail = typeof envelope.detail === "string" ? envelope.detail.trim() : "";
  return {
    key,
    params: { status: refusal.http_status, code },
    origin: "server",
    field: sentence ? "error.message" : detail ? "detail" : null,
    quote: sentence || detail || null,
    facts: refusal.facts,
  };
}

export function RecoveryRequiredPanel({ journeyId, recovery, flags, onRefresh }: Props) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);

  const stale = Boolean(flags?.staleness);
  const active = Boolean(recovery && recovery.state !== "RESOLVED");

  // The panel is never hidden by a local dismissal.  It only disappears when
  // the backend says the journey truth is actually healthy again.
  if (!stale && !active) return null;

  const noAlternative = Boolean(recovery?.no_alternative);
  const isAcknowledged = acknowledged || Boolean(recovery?.acknowledged);

  const failures = recovery?.failures ?? flags?.feasibility_failures ?? [];
  /* Which envelope field these came from, decided by the same `??` chain that picked the list --
     the fold has to name the field it is quoting, and a fixed label would be wrong half the time.
     The strings themselves are the backend's prose, so they leave the traveller's reading line and
     stay inside 技术详情 (Q64=① / Q67). Measured on the phone: a Chinese card whose only English
     line was this list. */
  const failureField = recovery?.failures ? "recovery.failures" : "flags.feasibility_failures";
  const title = stale
    ? t("recovery.staleTitle")
    : noAlternative
      ? t("recovery.noAltTitle")
      : t("recovery.decisionTitle");
  const detail = stale
    ? t("recovery.staleDetail")
    : noAlternative
      ? t("recovery.noAltDetail", { code: recovery?.capability_code ?? "—" })
      : t("recovery.decisionDetail", { code: recovery?.capability_code ?? "—" });

  async function replan(): Promise<void> {
    setBusy("REPLAN");
    setNotice(null);
    try {
      const response = await post(`/api/v1/journeys/${journeyId}/plans/generate`, {});
      if (!response.ok) {
        setNotice(await noticeFromRefusal("recovery.generateRefused", response));
        onRefresh();
        return;
      }
      setNotice({
        key: "recovery.generated",
        params: {},
        origin: "device",
        field: null,
        quote: null,
        facts: null,
      });
      onRefresh();
    } catch (error) {
      /* Only a transport exception may say the request never arrived; and even then the channel
         that decides it is shared with the rest of the app, so this card cannot claim a network
         cause the shell would contradict. */
      const attribution = classifyReadFailure(error);
      recordAttribution(attribution, "recovery.generate");
      setNotice({
        key: "recovery.generateRefused",
        params: {
          status: attribution.http_status ?? 0,
          code: attribution.reason,
        },
        origin: "device",
        field: null,
        quote: null,
        facts: null,
      });
    } finally {
      setBusy(null);
    }
  }

  async function acknowledge() {
    // Governed acknowledgement: this records that the traveller saw the
    // warning on the backend.  It never resolves recovery, never marks an
    // infeasible plan safe, and never hides the truth - the banner stays until
    // a revalidated feasible plan is actually selected.
    setBusy("ACK");
    setNotice(null);
    try {
      const response = await post(
        `/api/v1/journeys/${journeyId}/recovery/acknowledge`,
      );
      if (!response.ok) {
        setNotice(await noticeFromRefusal("recovery.ackRefused", response));
        return;
      }
      setAcknowledged(true);
      setNotice({
        key: "recovery.acknowledged",
        params: {},
        origin: "device",
        field: null,
        quote: null,
        facts: null,
      });
      onRefresh();
    } catch (error) {
      const attribution = classifyReadFailure(error);
      recordAttribution(attribution, "recovery.acknowledge");
      setNotice({
        key: "recovery.ackRefused",
        params: { status: attribution.http_status ?? 0, code: attribution.reason },
        origin: "device",
        field: null,
        quote: null,
        facts: null,
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className="v8-feasibility v8-recovery-required"
      data-testid="recovery-required-panel"
      data-recovery-state={recovery?.state ?? (stale ? "STALE_EVIDENCE" : "NONE")}
      role="alert"
      aria-live="polite"
    >
      <span className="status-dot" aria-hidden="true" />
      <div style={{ flex: 1 }}>
        <b data-testid="recovery-required-title">{title}</b>
        <small className="recovery-required-detail">{detail}</small>
        {failures.length > 0 && (
          <>
            <p className="v8-muted" data-testid="recovery-failure-count">
              {t("recovery.failureCount", { n: failures.length })}
            </p>
            <details className="v8-advanced" data-testid="recovery-failure-technical">
              <summary data-testid="recovery-failure-technical-toggle">
                {t("recovery.technicalDetail")}
              </summary>
              <span className="cnx-raw-facts-label" data-testid="recovery-failure-detail-kind">
                {t("recovery.serverReason")}
              </span>
              <ul className="recovery-failures" data-cnx-field={failureField}>
                {failures.slice(0, 4).map((item) => (
                  <li key={item} data-testid="recovery-failure">
                    {item}
                  </li>
                ))}
              </ul>
            </details>
          </>
        )}
        {/* A stale card has no recovery record, so acknowledging it answers 404 every time
            (measured: `no open recovery to acknowledge`).  A button that cannot succeed in the
            state that shows it is not a choice, so it is not mounted here -- what is owed to the
            reader instead is the sentence naming what actually clears the warning. */}
        {stale && !active && (
          <p className="v8-muted" data-testid="recovery-note">
            {t("recovery.noOpenRecovery")}
          </p>
        )}
        <div className="recovery-actions">
          <button
            type="button"
            className="v8-outline-button"
            data-testid="recovery-replan"
            onClick={() => void replan()}
            disabled={busy !== null}
          >
            {busy === "REPLAN" ? t("recovery.generating") : t("recovery.generate")}
          </button>
          {active && (
            <button
              type="button"
              className="v8-quiet-button"
              data-testid="recovery-acknowledge"
              onClick={() => void acknowledge()}
              disabled={busy !== null || isAcknowledged}
            >
              {isAcknowledged ? t("recovery.acknowledged") : t("recovery.acknowledge")}
            </button>
          )}
        </div>
        {notice && (
          <>
            <small className="recovery-message" data-testid="recovery-message" role="status">
              {t(notice.key, notice.params)}
            </small>
            {/* Q64=① + Q67: the fold may quote the answer, and the element doing the quoting names
                which envelope field the words are.  `data-cnx-field` carries the name only -- an
                attribute holding the sentence would move upstream words onto the screen by the side
                door (Q71).  A line this screen composed has no field to name and gets no anchor. */}
            {notice.quote && (
              <details className="v8-advanced" data-testid="recovery-technical">
                <summary data-testid="recovery-technical-toggle">{t("recovery.technicalDetail")}</summary>
                <span className="cnx-raw-facts-label" data-testid="recovery-detail-kind">
                  {notice.origin === "server"
                    ? t("recovery.serverReason")
                    : t("recovery.deviceReason")}
                </span>
                <code
                  data-testid="recovery-reason"
                  {...(notice.field ? { "data-cnx-field": notice.field } : {})}
                >
                  {notice.quote}
                </code>
              </details>
            )}
            {notice.facts && (
              <details className="v8-advanced" data-testid="recovery-raw-technical">
                <summary data-testid="recovery-raw-technical-toggle">{t("recovery.technicalDetail")}</summary>
                <RawFactsPanel facts={notice.facts} zh={zh} testId="recovery-raw-facts" />
              </details>
            )}
          </>
        )}
      </div>
    </section>
  );
}
