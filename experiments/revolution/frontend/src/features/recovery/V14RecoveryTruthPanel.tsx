import { useEffect, useState } from "react";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { formatMinorMoney } from "../../lib/money";
import { type RawFacts } from "../../lib/rawFacts";
import { rejectionWithFacts, RejectionWithFacts } from "../../lib/rejectionFacts";
import { CnxMeasuredFacts } from "../../components/CnxMeasuredFacts";
import {
  fetchWithWriteDeadline,
  isWriteDeadlineMissed,
  writeDeadlineCopy,
} from "../../lib/writeDeadline";

type Proposal = {
  id: string;
  status: string;
  proposed_plan_version_id: string;
  locked_commitments_preserved: boolean;
  diff: {
    removed?: string[];
    added?: string[];
    cost_delta_minor?: number;
    travel_delta_min?: number;
    verified_evidence?: {
      baseline_plan_evidence?: { snapshot_id?: string | null };
      incident_recovery_evidence?: {
        route_provenance?: string;
        source_provider?: string;
        mobility_snapshot?: MobilityEvidence;
      };
      impact_resolution?: {
        semantic_reason?: string;
        semantic_explanation?: string;
      };
      mobility?: { snapshot?: MobilityEvidence };
    };
  };
  explanation?: { impact_model?: string; impact_provider?: string };
};

type MobilityEvidence = {
  snapshot_id?: string;
  source_provider?: string;
  degraded?: boolean;
};

type Props = {
  journeyId: string;
  policy?: { mode: "ADVISORY" | "ASSISTED" | "AUTONOMOUS"; auto_accept_replan: boolean } | null;
  currency?: string;
  onAccepted?: () => void;
};

function moneyDelta(value: number | undefined, noChange: string, currency = "CNY"): string {
  if (value == null || value === 0) return noChange;
  return `${value > 0 ? "+" : "−"}${formatMinorMoney(Math.abs(value), currency, "en-US")}`;
}

export function V14RecoveryTruthPanel({ journeyId, policy, currency = "CNY", onAccepted }: Props) {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failureFacts, setFailureFacts] = useState<RawFacts | null>(null);
  const [failureEnvelope, setFailureEnvelope] = useState<unknown>(null);
  const { locale } = useLocale();
  const t = useT();
  const zh = locale.startsWith("zh");

  const refresh = async () => {
    const response = await fetch(`/api/v1/journeys/${journeyId}/replans`);
    if (!response.ok) return;
    const items = (await response.json()) as Proposal[];
    setProposal(items.find((item) => item.status === "PRESENTED") ?? items[0] ?? null);
  };

  useEffect(() => {
    void refresh();
  }, [journeyId]);

  const accept = async () => {
    if (!proposal) return;
    setBusy(true);
    try {
      const response = await fetchWithWriteDeadline(`/api/v1/replans/${proposal.id}/accept`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `v14-ui-${proposal.id}`,
        },
      });
      if (!response.ok) throw await rejectionWithFacts(response);
      await response.json();
      setMessage(t("v14.acceptedMessage"));
      await refresh();
      onAccepted?.();
    } catch (error) {
      /* ORD-0281: a bound this device ran out with is not a server statement and not a failed
         parse.  Raw English debug text used to be handed to the traveller here.  ORD-0345 closes the
         remaining half of that: an answered refusal also printed the server's own sentence, so the
         fallthrough now names only what this device knows -- no confirmable answer, result unknown --
         and the diagnosable part is the reading this device took, in the fold below. */
      setFailureFacts(error instanceof RejectionWithFacts ? error.facts : null);
      setFailureEnvelope(error instanceof RejectionWithFacts ? error.envelope : null);
      setMessage(
        isWriteDeadlineMissed(error)
          ? `${writeDeadlineCopy(false).title} ${writeDeadlineCopy(false).body}`
          : zh
            ? "这步接受没有得到可确认的答复，结果未知。"
            : "This acceptance got no confirmable answer; the result is unknown.",
      );
    } finally {
      setBusy(false);
    }
  };

  const verified = proposal?.diff.verified_evidence;
  const incident = verified?.incident_recovery_evidence;
  const snapshot = incident?.mobility_snapshot ?? verified?.mobility?.snapshot;
  const modeLabel = (mode: string) => {
    const key = `v14.mode.${mode}`;
    const value = t(key);
    return value === key ? t("v14.modeUnknown", { code: mode }) : value;
  };
  const authorization = policy
    ? `${modeLabel(policy.mode)} · ${policy.auto_accept_replan ? t("v14.autoAcceptOn") : t("v14.confirmationRequired")}`
    : t("v14.policyUnavailable");
  return (
    <section className="v12-authoritative-panel" data-testid="v14-recovery-truth">
      <header className="v12-panel-header">
        <div>
          <span className="v8-kicker">{t("v14.kicker")}</span>
          <h2>{t("v14.title")}</h2>
        </div>
        <button className="v8-outline-button" type="button" onClick={() => void refresh()}>
          {t("v14.refresh")}
        </button>
      </header>
      {!proposal && <p className="v8-muted">{t("v14.noProposal")}</p>}
      {proposal && (
        <>
          <div className="v12-now-next">
            <article className="v12-state-card now">
              <span className="v12-state-kicker">{t("v14.kickerChanged")}</span>
              <strong data-testid="v14-recovery-changed">
                {proposal.diff.removed?.[0] ?? t("recovery.changedFromActivity")}
              </strong>
              <small>→ {proposal.diff.added?.[0] ?? t("recovery.changedToActivity")}</small>
            </article>
            <article className="v12-state-card next">
              <span className="v12-state-kicker">{t("v14.kickerProtected")}</span>
              <strong data-testid="v14-recovery-protected">
                {proposal.locked_commitments_preserved ? t("v14.c4Preserved") : t("v14.reviewCommitmentImpact")}
              </strong>
              <small>{moneyDelta(proposal.diff.cost_delta_minor, t("v14.noCostChange"), currency)} · {t("v14.routeDelta", { n: proposal.diff.travel_delta_min ?? 0 })}</small>
            </article>
          </div>
          <div className="v8-cost-lines">
            <div>
              <span>{t("v14.baselineEvidence")}</span>
              <b>{verified?.baseline_plan_evidence?.snapshot_id ?? "—"}</b>
            </div>
            <div>
              <span>{t("v14.incidentEvidence")}</span>
              <b data-testid="v14-recovery-provider">
                {incident?.source_provider ?? snapshot?.source_provider ?? "—"} · {incident?.route_provenance ?? "—"}
              </b>
            </div>
            <div>
              <span>{t("v14.deterministicGate")}</span>
              <b>{snapshot?.degraded ? t("v14.gateDegraded") : t("v14.gatePassed")}</b>
            </div>
            <div>
              <span>{t("v14.delegationPolicy")}</span>
              <b data-testid="v14-recovery-authorization">{authorization}</b>
            </div>
          </div>
          <p className="v8-muted">
            {verified?.impact_resolution?.semantic_explanation ??
              verified?.impact_resolution?.semantic_reason ??
              t("v14.impactFallback", {
                provider: proposal.explanation?.impact_provider ?? "CityNexus",
                model: proposal.explanation?.impact_model ?? t("v14.impactModelDeterministic"),
              })}
          </p>
          {message && (
            <>
              <p role="status" data-testid="v14-recovery-message">{message}</p>
              {/* Q70/Q71: one element per contract field, named by the field, values measured here --
                  and no fragment of the answer's text, in the attributes either. */}
              {failureFacts && (
                <details className="v14-recovery-technical" data-testid="v14-recovery-technical">
                  <summary data-testid="v14-recovery-technical-toggle">
                    {zh ? "技术详情" : "Technical detail"}
                  </summary>
                  <CnxMeasuredFacts
                    facts={failureFacts}
                    zh={zh}
                    testId="v14-recovery-fact"
                    envelope={failureEnvelope}
                  />
                </details>
              )}
            </>
          )}
          {proposal.status === "PRESENTED" && (
            <button
              className="v8-primary"
              type="button"
              data-testid="v14-recovery-accept"
              disabled={busy}
              onClick={() => void accept()}
            >
              {busy ? t("v14.applying") : t("v14.accept")}
            </button>
          )}
          {proposal.status === "ACCEPTED" && (
            <span className="v8-status-pill" data-testid="v14-recovery-accepted">
              {t("v14.acceptedChild", { id: proposal.proposed_plan_version_id })}
            </span>
          )}
        </>
      )}
    </section>
  );
}
