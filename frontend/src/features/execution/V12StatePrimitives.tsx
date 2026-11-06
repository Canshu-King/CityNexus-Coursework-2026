import type { ReactNode } from "react";
import { AlertTriangle, Check, CloudOff, LockKeyhole, Radio, ShieldCheck } from "lucide-react";
import type { ExecutionState } from "./V12AuthoritativeJourneyPanel";
import { useT } from "../../i18n/LocaleContext";

const stateKeyMap: Record<string, string> = {
  NOT_STARTED: "exec.state.notStarted",
  EN_ROUTE: "exec.state.enRoute",
  ARRIVED: "exec.state.arrived",
  IN_PROGRESS: "exec.state.inProgress",
  ACTIVE: "exec.state.active",
  COMPLETED: "exec.state.completed",
  SKIPPED: "exec.state.skipped",
};

function stateLabel(state: string | undefined, t: (key: string) => string): string {
  if (!state) return t("exec.state.notStarted");
  const key = stateKeyMap[state];
  return key ? t(key) : state.replaceAll("_", " ");
}

export function ActivityCard({ title, state, locked = false }: { title: string; state: ExecutionState; locked?: boolean }) {
  const t = useT();
  return <article className="v12-activity-row"><span className="v12-activity-index">{state === "COMPLETED" ? <Check size={14} /> : "•"}</span><span className="v12-activity-title">{title}</span><span className="v12-activity-status">{stateLabel(state, t)}{locked && <LockKeyhole size={12} />}</span></article>;
}

export function JourneyLeg({ mode, durationMin }: { mode: string; durationMin: number }) {
  const t = useT();
  return <div className="v12-leg-chip"><b>{mode}</b><span>{t("exec.min", { n: durationMin })}</span></div>;
}

export function CommitmentCard({ title, level = "C4_Anchored" }: { title: string; level?: string }) {
  const t = useT();
  return <article className="v12-commitment-card"><ShieldCheck size={15} /><div><b>{title}</b><small>{level} {t("exec.protectedSuffix")}</small></div></article>;
}

export function ImpactPreview({ passed, summary }: { passed: boolean; summary: string }) {
  const t = useT();
  return <div className={`v12-impact-card ${passed ? "passed" : "blocked"}`}><span>{passed ? <Check size={15} /> : <AlertTriangle size={15} />}</span><div><b>{passed ? t("exec.impactPassed") : t("exec.impactBlocked")}</b><small>{summary}</small></div></div>;
}

export function RiskBanner({ risk, children }: { risk: "SAFE" | "AT_RISK" | "CRITICAL"; children: ReactNode }) {
  return <div className={`v12-risk-banner risk-${risk.toLowerCase()}`}><AlertTriangle size={16} /><div><b>{risk.replaceAll("_", " ")}</b><span>{children}</span></div></div>;
}

export function AgentAction({ label, decision }: { label: string; decision: string }) {
  const t = useT();
  return <div className="v12-agent-action"><Radio size={15} /><div><b>{label}</b><small>{t("exec.policyDecision")} {decision}</small></div></div>;
}

export function OfflineBanner({ readOnly = true }: { readOnly?: boolean }) {
  const t = useT();
  return <div className="v12-offline-banner"><CloudOff size={16} /><span>{readOnly ? t("exec.offlineReadOnly") : t("exec.offlineQueueReady")}</span></div>;
}

export function EdgeStatus({ status = "SYNCED" }: { status?: "SYNCED" | "STALE" | "DEGRADED" }) {
  const t = useT();
  return <div className="v12-edge-status"><Radio size={15} /><span>{t("exec.picEdge")} {status}</span></div>;
}

export function LiveJourney({ now, next, state }: { now: string; next: string; state: ExecutionState }) {
  const t = useT();
  return <div className="v12-live-journey"><span className="v8-kicker">{t("exec.liveJourneyPrefix")} {stateLabel(state, t)}</span><strong>{now}</strong><small>{t("exec.nextPrefix")} {next}</small></div>;
}
