import { useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n/LocaleContext";

/* V14 · PREVENTIVE owner (pass-16 D/E).
 *
 * `V14RecoveryTruthPanel` is the reactive half: reality already changed, a
 * proposal exists, and the traveller (or the envelope) accepts it.  Nothing in
 * V8 owned the other half, even though the backend has implemented it since v6:
 * a forecast pulse produces a `GuardianRun` with `decision=PREEMPTIVE_RECOVERY`
 * and `trigger_kind=PREDICTIVE` *before* any hard failure, and that run changes
 * the authoritative plan.
 *
 * This panel is deliberately NOT a relabelled recovery.  It reads the guardian
 * run itself, so every line is bound to durable evidence rather than to a plan
 * diff, and it distinguishes the two on the axis that matters:
 *   - trigger: a forecast, not an event;
 *   - timing: before the failure, `forecast_before_hard_failure`;
 *   - authority: executed autonomously inside the policy envelope, or gated.
 *
 * The testids are structured (one fact per node, plus run/plan ids as data
 * attributes) so an acceptance case can assert the evidence fields directly
 * instead of pattern-matching body text.
 */

type ResilienceSnapshot = {
  id?: string;
  status?: string | null;
  score?: number | null;
  risk_score?: number | null;
  components?: Record<string, number> | null;
  dominant_failure_modes?: { label?: string; place_id?: string | null }[] | null;
  evidence_provenance?: string[] | null;
};

type GuardianRun = {
  id: string;
  decision: string;
  status: string;
  action_status: string;
  trigger_kind: string | null;
  trigger_event_id: string | null;
  proposal_id: string | null;
  reasoning_summary?: string | null;
  active_plan_before_id: string | null;
  active_plan_after_id: string | null;
  created_at?: string | null;
  evidence?: {
    observation?: {
      trigger_kind?: string;
      forecast_before_hard_failure?: boolean;
      predictive_event_id?: string | null;
    };
    prediction?: ResilienceSnapshot | null;
    inference?: { decision?: string; reason?: string; controller?: string };
    solver?: { kind?: string } | null;
    policy?: { decision?: string; reason?: string; mode?: string } | null;
  };
};

type Experience = {
  active_plan?: {
    id?: string;
    activities?: {
      id: string;
      place_id?: string | null;
      title?: string | null;
      locked?: boolean;
      protected?: boolean;
    }[];
  };
};

type Props = { journeyId: string; refreshKey?: unknown };

const PREVENTIVE = (run: GuardianRun) =>
  run.decision === "PREEMPTIVE_RECOVERY" && run.trigger_kind === "PREDICTIVE";

async function readJson<T>(url: string): Promise<T | null> {
  try {
    const response = await fetch(url);
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export function V14PreventivePanel({ journeyId, refreshKey }: Props) {
  const t = useT();
  const [run, setRun] = useState<GuardianRun | null>(null);
  const [experience, setExperience] = useState<Experience | null>(null);
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      const status = await readJson<{ runs?: GuardianRun[] }>(
        `/api/v1/journeys/${journeyId}/guardian/status`,
      );
      /* Newest by created_at, not runs[0]: the endpoint has never promised an
         ordering, and "the run the panel is describing" must not depend on
         guessing which end of the list is current. */
      const preventive = (status?.runs ?? [])
        .filter(PREVENTIVE)
        .reduce<GuardianRun | null>(
          (newest, item) =>
            !newest || String(item.created_at ?? "") > String(newest.created_at ?? "")
              ? item
              : newest,
          null,
        );
      if (cancelled) return;
      setAvailable(preventive !== null);
      setRun(preventive);
      if (!preventive) return;
      setExperience(await readJson<Experience>(`/api/v1/journeys/${journeyId}/experience`));
      /* A run is written as RUNNING / PLANNING before the recomposition lands and
         only later carries COMPLETED / EXECUTED, so "a run exists" is not "the
         story is finished".  Stopping here on mere existence would freeze a
         traveller who opened the page mid-action on a half-finished claim until
         they reloaded by hand.  FAILED_RETRYABLE keeps polling too: a retry can
         still change what this panel is asserting. */
      if (preventive.status === "COMPLETED" && timer !== undefined) {
        window.clearInterval(timer);
        timer = undefined;
      }
    };
    void load();
    timer = window.setInterval(() => void load(), 3000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [journeyId, refreshKey]);

  const protectedPlaces = useMemo(
    () => (experience?.active_plan?.activities ?? []).filter((item) => item.protected),
    [experience],
  );

  if (!available || !run) return null;

  const prediction = run.evidence?.prediction ?? null;
  const preFailure = run.evidence?.observation?.forecast_before_hard_failure === true;
  const solver = run.evidence?.solver?.kind ?? "unavailable";
  const score = prediction?.risk_score ?? prediction?.score ?? null;
  const planChanged =
    !!run.active_plan_before_id &&
    !!run.active_plan_after_id &&
    run.active_plan_before_id !== run.active_plan_after_id;
  /* "Nothing had failed yet" is only true if this run was not started by a hard
     event AND no CLOSURE/CRITICAL event preceded it.  The backend records
     `predictive_prevented` on the forecast signal rather than a failure event;
     a null trigger_event_id is the honest marker of a pure forecast pulse. */
  return (
    <section className="v12-authoritative-panel v14-preventive" data-testid="v14-preventive">
      <header className="v12-panel-header">
        <div>
          <span className="v8-kicker">V14 · BEFORE REALITY CHANGED</span>
          <h2>Preventive recovery</h2>
          <small>
            A forecast, not a failure, triggered this. Reactive recoveries are owned by the
            grounded adaptive recovery panel below.
          </small>
        </div>
        <span className="v8-status-pill" data-testid="v14-preventive-timing" data-pre-failure={preFailure}>
          {preFailure ? "acted before the failure" : "acted after a failure"}
        </span>
      </header>

      <div className="v12-now-next">
        <article className="v12-state-card now" data-testid="v14-preventive-trigger" data-run-id={run.id}>
          <span className="v12-state-kicker">TRIGGER</span>
          <strong>{run.trigger_kind ?? "—"}</strong>
          <small>
            decision{" "}
            <b data-testid="v14-preventive-decision">{run.decision}</b> ·{" "}
            <span data-testid="v14-preventive-action">{run.action_status}</span>
          </small>
        </article>
        <article className="v12-state-card next">
          <span className="v12-state-kicker">FORECAST</span>
          <strong data-testid="v14-preventive-resilience">
            {prediction?.status ?? t("recovery.statusUnavailable")}
            {score != null ? ` · risk ${score}` : ""}
            {prediction?.components?.forecast_risk != null
              ? ` · forecast ${prediction.components.forecast_risk}`
              : ""}
          </strong>
          <small data-testid="v14-preventive-evidence">
            solver {solver} · snapshot {prediction?.id ?? "—"} ·{" "}
            {(prediction?.dominant_failure_modes ?? [])
              .map((mode) => mode.label ?? mode.place_id ?? "?")
              .slice(0, 2)
              .join(", ") || "no dominant mode"}
          </small>
        </article>
      </div>

      <div className="v8-cost-lines">
        <div>
          <span>Why it acted</span>
          <b data-testid="v14-preventive-reason">
            {run.evidence?.inference?.reason ?? run.reasoning_summary ?? "—"}
          </b>
        </div>
        <div>
          <span>Plan</span>
          <b
            data-testid="v14-preventive-plan-change"
            data-plan-before-id={run.active_plan_before_id ?? ""}
            data-plan-after-id={run.active_plan_after_id ?? ""}
            data-changed={planChanged}
          >
            {planChanged
              ? `recomposed ${run.active_plan_before_id?.slice(0, 8)}… → ${run.active_plan_after_id?.slice(0, 8)}…`
              : "unchanged · action stayed inside the envelope"}
          </b>
        </div>
        <div>
          <span>Protected commitments</span>
          <b data-testid="v14-preventive-protected" data-count={protectedPlaces.length}>
            {protectedPlaces.length
              ? `${protectedPlaces.length} preserved: ${protectedPlaces
                  .map((item) => item.place_id ?? item.id)
                  .join(", ")}`
              : "none on this journey"}
          </b>
        </div>
        <div>
          <span>Authorization</span>
          <b data-testid="v14-preventive-policy">
            {run.evidence?.policy?.decision ?? run.evidence?.inference?.controller ?? t("recovery.guardianEnvelope")}
          </b>
        </div>
      </div>
    </section>
  );
}
