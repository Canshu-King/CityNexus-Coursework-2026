import { useMemo, useState } from "react";
import { Check, ChevronRight, Circle, LockKeyhole, MapPin, ShieldCheck } from "lucide-react";
import { transitionActivity, type PlanVersion } from "../../api/v12Authoritative";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { executionStateLabel } from "../../lib/displayVocabulary";

export type ExecutionState =
  | "NOT_STARTED"
  | "EN_ROUTE"
  | "ARRIVED"
  | "IN_PROGRESS"
  | "ACTIVE"
  | "COMPLETED"
  | "SKIPPED";

export type ExecutionActivity = {
  id: string;
  sequence_no: number;
  title: string;
  activity_type?: string;
  status?: ExecutionState;
  start_at?: string;
  end_at?: string;
  locked?: boolean;
};

type Props = {
  journeyId?: string;
  plan: Pick<PlanVersion, "id" | "activities" | "mobility_snapshot_id"> | null;
  nowActivityId?: string | null;
  nextActivityId?: string | null;
  offline?: boolean;
  onTransition?: (activityId: string, state: ExecutionState) => void;
};

const transitions: Record<string, "depart" | "arrive" | "begin" | "complete"> = {
  NOT_STARTED: "depart",
  ACTIVE: "depart",
  EN_ROUTE: "arrive",
  ARRIVED: "begin",
  IN_PROGRESS: "complete",
};

const stateKeyMap: Record<string, string> = {
  NOT_STARTED: "exec.state.notStarted",
  EN_ROUTE: "exec.state.enRoute",
  ARRIVED: "exec.state.arrived",
  IN_PROGRESS: "exec.state.inProgress",
  ACTIVE: "exec.state.active",
  COMPLETED: "exec.state.completed",
  SKIPPED: "exec.state.skipped",
};

function label(
  state: string | undefined,
  t: (key: string) => string,
  locale: string,
): string {
  if (!state) return t("exec.state.notStarted");
  const key = stateKeyMap[state];
  return key ? t(key) : executionStateLabel(state, locale);
}

export function V12AuthoritativeJourneyPanel({
  journeyId,
  plan,
  nowActivityId,
  nextActivityId,
  offline = false,
  onTransition,
}: Props) {
  const t = useT();
  const { locale } = useLocale();
  const [busy, setBusy] = useState(false);
  const activities = useMemo(
    () => (plan?.activities ?? []) as ExecutionActivity[],
    [plan],
  );
  const now = activities.find((item) => item.id === nowActivityId) ??
    activities.find((item) => !["COMPLETED", "SKIPPED"].includes(item.status ?? ""));
  const next = activities.find((item) => item.id === nextActivityId) ??
    activities.find((item) => item.id !== now?.id && !["COMPLETED", "SKIPPED"].includes(item.status ?? ""));
  const action = now ? transitions[now.status ?? "NOT_STARTED"] : undefined;
  const advance = async () => {
    if (!journeyId || !now || !action || offline) return;
    setBusy(true);
    try {
      const twin = await transitionActivity(journeyId, now.id, action);
      const state = (twin.context as { execution?: { now_state?: ExecutionState } }).execution?.now_state;
      onTransition?.(now.id, state ?? now.status ?? "NOT_STARTED");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="v12-authoritative-panel" data-testid="v12-authoritative-panel">
      <header className="v12-panel-header">
        <div>
          <span className="v8-kicker">{t("exec.authoritativeJourney")}</span>
          <h2>{t("exec.oneTruth")}</h2>
        </div>
        <span className="v12-evidence-badge">
          <ShieldCheck size={14} /> {plan?.mobility_snapshot_id ? t("exec.frozenEvidence") : t("exec.evidencePending")}
        </span>
      </header>
      <div className="v12-now-next">
        <article className="v12-state-card now">
          <span className="v12-state-kicker"><Circle size={12} /> {t("exec.now")}</span>
          <strong>{now?.title ?? t("exec.journeyComplete")}</strong>
          <span className="v12-state-value">{label(now?.status, t, locale)}</span>
          {now?.locked && <small><LockKeyhole size={13} /> {t("exec.protectedAnchor")}</small>}
        </article>
        <ChevronRight className="v12-state-arrow" aria-hidden="true" />
        <article className="v12-state-card next">
          <span className="v12-state-kicker"><MapPin size={12} /> {t("exec.next")}</span>
          <strong>{next?.title ?? t("exec.noNextStep")}</strong>
          <span className="v12-state-value">{label(next?.status, t, locale)}</span>
          {next?.start_at && <small>{new Date(next.start_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small>}
        </article>
      </div>
      <div className="v12-activity-rail" aria-label={t("exec.railLabel")}>
        {activities.map((item) => (
          <div className={`v12-activity-row ${item.id === now?.id ? "current" : ""}`} key={item.id}>
            <span className="v12-activity-index">{item.status === "COMPLETED" || item.status === "SKIPPED" ? <Check size={14} /> : item.sequence_no}</span>
            <span className="v12-activity-title">{item.title}</span>
            <span className={`v12-activity-status state-${(item.status ?? "NOT_STARTED").toLowerCase()}`}>{label(item.status, t, locale)}</span>
          </div>
        ))}
      </div>
      <footer className="v12-panel-footer">
        <span>{offline ? t("exec.offlineSnapshot") : t("exec.serverTruth")}</span>
        <button className="v8-primary" type="button" disabled={busy || !action || offline} onClick={() => void advance()}>
          {busy ? t("exec.updating") : action ? `${t("exec.markInProgress").split(" ")[0]} ${label(action === "depart" ? "EN_ROUTE" : action === "arrive" ? "ARRIVED" : action === "begin" ? "IN_PROGRESS" : "COMPLETED", t, locale)}` : t("exec.complete")}
        </button>
      </footer>
    </section>
  );
}
