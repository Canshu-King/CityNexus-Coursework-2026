import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, LockKeyhole, WandSparkles } from "lucide-react";
import {
  useAcceptTripStudioEdit,
  useTripStudioPreview,
  type PlanVersion,
  type TripStudioPreview,
} from "../../api/v12Authoritative";
import { ImpactPreview } from "../execution/V12StatePrimitives";
import { SortableActivityList } from "./SortableActivityList";
import { RawFactsPanel } from "../../components/RawFactsPanel";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { planSourceLabel, planVersionNoun } from "../../lib/displayVocabulary";
import { RejectionWithFacts } from "../../lib/rejectionFacts";
import { formatV8Money } from "../../lib/v8Formatting";

/**
 * The refusal names this panel can explain, taken from the server's own raise sites
 * (`backend/app/main.py:5395`, `:5397`, `:5495`, `:5499`, `:5513`). A token outside this set is
 * counted, never translated -- inventing a reason is the defect this file is being fixed for.
 */
const GATE_CODES = [
  "PROTECTED_ACTIVITY_REORDER",
  "FROZEN_ROUTE_EVIDENCE_MISSING",
  "FROZEN_EVIDENCE_INTEGRITY_MISMATCH",
  "STALE_FROZEN_EVIDENCE",
  "INVALID_FROZEN_EVIDENCE_EXPIRY",
  "MOBILITY_DATA_UNAVAILABLE",
] as const;

type Props = {
  journeyId: string;
  plan: PlanVersion;
  onAccepted?: () => void;
};

export function V12TripStudioPanel({ journeyId, plan, onAccepted }: Props) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const initialOrder = useMemo(() => plan.activities.map((item) => item.id), [plan.activities]);
  const [draftOrder, setDraftOrder] = useState(initialOrder);
  const [preview, setPreview] = useState<TripStudioPreview | null>(null);
  const previewMutation = useTripStudioPreview(journeyId);
  const acceptMutation = useAcceptTripStudioEdit(journeyId);
  const names = useMemo(
    () => new Map(plan.activities.map((item) => [item.id, item.title])),
    [plan.activities],
  );
  const labels = useMemo(
    () => Object.fromEntries(names.entries()),
    [names],
  );
  const changed = draftOrder.some((id, index) => id !== initialOrder[index]);
  const protectedCount = plan.activities.filter((item) => item.locked).length;
  /* Every activity on the sealed demo's chosen plan is a protected anchor (measured: 5 of 5), which
     means no reorder can ever pass `PROTECTED_ACTIVITY_REORDER`. Saying so before the traveller
     drags is the honest reading; the alternative was a disabled 接受编辑 with no sentence anywhere
     explaining that the draft was doomed from the start. */
  const nothingToMove = plan.activities.length > 0 && protectedCount === plan.activities.length;
  const currency = plan.activities[0]?.estimated_cost?.currency ?? "CNY";

  useEffect(() => {
    setDraftOrder(initialOrder);
    setPreview(null);
  }, [initialOrder, plan.id]);

  const previewDraft = async () => {
    const next = await previewMutation.mutateAsync({ activity_ids: draftOrder, draft_order: draftOrder });
    setPreview(next);
  };

  const acceptDraft = async () => {
    if (!preview?.can_accept) return;
    await acceptMutation.mutateAsync({ activity_ids: draftOrder, preview_id: preview.preview_id });
    setPreview(null);
    onAccepted?.();
  };

  /** The server's own refusal list, split into what this screen can name and what it cannot.
   *  Per-leg refusals arrive as `MOBILITY_DATA_UNAVAILABLE:<from>|<to>` -- the leg pair is data the
   *  traveller cannot act on, so it is counted into `{n}` and the raw line stays in the fold. */
  const rawFailures = useMemo(() => {
    const list = (preview?.deterministic_gate as { failures?: unknown } | undefined)?.failures;
    return Array.isArray(list) ? list.map(String) : [];
  }, [preview]);
  const gateReasons = useMemo(() => {
    const counts = new Map<string, number>();
    let unnamed = 0;
    for (const item of rawFailures) {
      const code = item.split(":", 1)[0];
      if ((GATE_CODES as readonly string[]).includes(code as (typeof GATE_CODES)[number])) {
        counts.set(code, (counts.get(code) ?? 0) + 1);
      } else {
        unnamed += 1;
      }
    }
    const lines = [...counts.entries()].map(([code, n]) => ({
      key: code,
      text: t(`studio.gate.${code}`, { n }),
    }));
    if (unnamed > 0) lines.push({ key: "UNNAMED", text: t("studio.gate.unknown", { n: unnamed }) });
    return lines;
  }, [rawFailures, t]);
  const requestFailure = previewMutation.error ?? acceptMutation.error;
  const requestFacts = requestFailure instanceof RejectionWithFacts ? requestFailure.facts : null;

  return (
    <section className="v12-trip-studio" data-testid="v12-trip-studio">
      <header className="v12-panel-header">
        <div>
          <span className="v8-kicker">{t("studio.v12Kicker")}</span>
          <h2>{t("studio.v12Title")}</h2>
          <p className="v8-muted">{t("studio.v12Sub")}</p>
        </div>
        <span className="v12-evidence-badge"><WandSparkles size={14} /> {plan.mobility_snapshot_id ? t("studio.sameEvidence") : t("studio.evidencePending")}</span>
      </header>
      <div className="v12-studio-meta">
        <span>{t("fmt.activities", { n: plan.activities.length })}</span>
        <span>{t("fmt.protectedAnchors", { n: protectedCount })}</span>
        <span>{changed ? t("studio.draftDiffers") : t("studio.noDraft")}</span>
      </div>
      <SortableActivityList
        activities={draftOrder}
        labels={labels}
        protectedFirst={Boolean(plan.activities[0]?.locked)}
        onDraft={(next) => {
          setDraftOrder(next);
          setPreview(null);
        }}
      />
      {/* Reordering is the only edit this panel offers and nothing on screen said so (measured
          `cursor: auto`, no hint, one PointerSensor). The warning goes up before the drag, not
          after the refusal. */}
      <p className="v8-muted" data-testid="v12-studio-drag-hint">
        {t("studio.dragHint")}
      </p>
      {nothingToMove && (
        <p className="v12-studio-blocked" data-testid="v12-studio-all-protected" role="status">
          {t("studio.allProtected", { n: protectedCount })}
        </p>
      )}
      <div className="v12-studio-legend">
        <span><LockKeyhole size={13} /> {t("studio.lockedLegend")}</span>
        <span>{t("studio.previewLegend")}</span>
      </div>
      {preview && (
        <div data-testid="v12-impact-preview">
          <ImpactPreview
            passed={preview.can_accept}
            summary={preview.can_accept ? t("studio.impactPass") : t("studio.impactFail")}
          />
          {!preview.can_accept && gateReasons.length > 0 && (
            <>
              <p className="v12-studio-reasons-head" data-testid="v12-gate-reasons-head">
                {t("studio.blockedReasons")}
              </p>
              <ul className="v12-studio-reasons" data-testid="v12-gate-reasons">
                {gateReasons.map((reason) => (
                  <li key={reason.key} data-testid="v12-gate-reason">{reason.text}</li>
                ))}
              </ul>
            </>
          )}
          {!preview.can_accept && rawFailures.length > 0 && (
            <details className="v8-advanced" data-testid="v12-gate-technical">
              <summary data-testid="v12-gate-technical-toggle">{t("studio.technicalDetail")}</summary>
              {/* Q67: the fold may carry the server's own words, and the element carrying them names
                  which envelope field they are. Only the field name is an anchor -- an attribute with
                  the words in it would put them back on the screen (Q71). */}
              <span className="cnx-raw-facts-label" data-testid="v12-gate-detail-kind">
                {t("recovery.serverReason")}
              </span>
              <code
                data-testid="v12-gate-raw"
                data-cnx-field="deterministic_gate.failures"
              >{rawFailures.join("\n")}</code>
            </details>
          )}
          {preview.can_accept && <p className="v12-studio-success"><CheckCircle2 size={14} /> {t("studio.readyNext")}</p>}
          <small className="v8-muted" data-testid="v13-frozen-evidence-summary">
            {t("studio.frozenEvidence")} {preview.evidence_snapshot_id ?? t("studio.snapshotPending")} · {" "}
            {t("studio.costDelta", {
              v: formatV8Money(
                (preview.impact as { cost_delta_minor?: number }).cost_delta_minor ?? 0,
                currency,
                locale,
              ),
            })} · {t("studio.timeDelta", {
              n: String((preview.impact as { time_delta_min?: number }).time_delta_min ?? 0),
            })}
          </small>
        </div>
      )}
      <footer className="v12-panel-footer">
        <span>{`${planVersionNoun(locale)} ${plan.version_no} · ${planSourceLabel(plan.source, locale)}`}</span>
        <div className="v12-studio-actions">
          <button className="v8-outline-button" type="button" data-testid="v12-preview-impact" disabled={!changed || previewMutation.isPending} onClick={() => void previewDraft()}>
            {previewMutation.isPending ? t("studio.checking") : t("studio.previewImpact")}
          </button>
          <button className="v8-primary" type="button" data-testid="v12-accept-edit" disabled={!preview?.can_accept || acceptMutation.isPending} onClick={() => void acceptDraft()}>
            {acceptMutation.isPending ? t("studio.accepting") : t("studio.acceptEdit")}
          </button>
        </div>
      </footer>
      {requestFailure && (
        <>
          {/* `(previewMutation.error ?? acceptMutation.error)?.message` used to render the backend's
              sentence as the traveller's explanation. What this device measured goes in the fold. */}
          <p className="v12-studio-error" role="alert" data-testid="v12-request-error">
            <AlertTriangle size={14} /> {t("studio.requestRefused", { status: requestFacts?.http_status ?? 0 })}
          </p>
          {requestFacts && (
            <details className="v8-advanced" data-testid="v12-request-technical">
              <summary data-testid="v12-request-technical-toggle">{t("studio.technicalDetail")}</summary>
              <RawFactsPanel facts={requestFacts} zh={zh} testId="v12-request-facts" />
            </details>
          )}
        </>
      )}
    </section>
  );
}
