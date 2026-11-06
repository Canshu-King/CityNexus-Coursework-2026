import {
  Activity,
  Clock3,
  ExternalLink,
  ShieldCheck,
  WifiOff,
} from "lucide-react";
import {
  useExecutionClock,
  useWebObservations,
} from "../../api/v10Execution";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { clockZoneLabel } from "../../lib/displayVocabulary";
import { liveActivityWindow } from "../../lib/executionWindow";
import type { V8Data } from "../../app/v8Types";

type Props = {
  journeyId: string;
  /**
   * The ACTIVE plan as the projection served it.  "In progress" is a statement about the
   * resolved execution window, so the panel takes the plan and resolves it here rather than
   * reading the persisted cursor, which only records where the pointer was left.
   */
  plan: V8Data["active_plan"];
};

export function V10LiveExecutionPanel({ journeyId, plan }: Props) {
  const t = useT();
  const { locale } = useLocale();
  const clock = useExecutionClock(journeyId);
  const web = useWebObservations(journeyId);
  const now = clock.data ? new Date(clock.data.now) : null;
  const window = liveActivityWindow(plan);
  // Four facts, four answers.  The old pair of branches keyed off "does a cursor exist",
  // which made a stale cursor read as "in progress" and a finished journey read as
  // "awaiting plan" - both wrong, and both in the same metric.
  const stepLabel = window.unresolved
    ? t("live.unresolvedStep")
    : window.now
      ? t("exec.inProgress")
      : plan
        ? t("live.nothingDue")
        : t("live.awaitingPlan");
  return (
    <section
      className="v10-execution-panel"
      data-testid="v10-live-execution-panel"
    >
      <div className="v10-execution-heading">
        <div>
          <span className="v8-kicker">{t("exec.liveStatus")}</span>
          <h3>{t("exec.whatHappening")}</h3>
        </div>
        <span className="v10-truth-pill">
          <ShieldCheck size={14} /> {t("exec.updatedNow")}
        </span>
      </div>
      <div className="v10-execution-grid">
        <div className="v10-metric">
          <Clock3 size={17} aria-hidden="true" />
          <span>{t("exec.clock")}</span>
          <strong>
            {now
              ? now.toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })
              : "—"}
          </strong>
          <small>{clock.data ? clockZoneLabel(clock.data.timezone, locale) : t("exec.execClock")}</small>
        </div>
        <div className="v10-metric">
          <Activity size={17} aria-hidden="true" />
          <span>{t("exec.currentStep")}</span>
          <strong data-testid="v10-current-step" data-cursor-status={window.cursorStatus ?? "UNKNOWN"}>
            {stepLabel}
          </strong>
          <small>{clock.data ? t("exec.journeyStatus") : t("exec.loading")}</small>
        </div>
        <div className="v10-metric">
          <WifiOff size={17} aria-hidden="true" />
          <span>{t("exec.offline")}</span>
          <strong>{t("exec.readOnlyReady")}</strong>
          <small>{t("exec.next24hSaved")}</small>
        </div>
      </div>
      <details className="v10-evidence-drawer">
        <summary>
          {t("exec.evidenceTrail", { n: web.data?.observations.length ?? 0 })}{" "}
          <span>
            {web.data?.observations.length
              ? t("exec.evidenceTrail", { n: web.data.observations.length })
              : t("exec.noEvidence")}
          </span>
        </summary>
        {web.data?.observations.length ? (
          web.data.observations.slice(0, 3).map((item) => (
            <a
              className="v10-evidence-row"
              key={item.id}
              href={item.url}
              target="_blank"
              rel="noreferrer"
            >
              <span>
                <b>{item.title}</b>
                <small>
                  {item.domain} · {item.source_class}
                </small>
              </span>
              <ExternalLink size={14} aria-hidden="true" />
            </a>
          ))
        ) : (
          <p className="v10-muted">
            {t("exec.noWebObs")}
          </p>
        )}
      </details>
    </section>
  );
}
