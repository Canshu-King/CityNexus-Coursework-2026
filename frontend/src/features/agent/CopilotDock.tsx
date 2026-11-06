import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import type { V8Data } from "../../app/v8Types";
import {
  cityNexusApi,
  type AgentMissionPlan,
  type CopilotResponse,
  type NativeRuntimeEvent,
} from "../../api/citynexus";
import { useLocale, useT } from "../../i18n/LocaleContext";
import { AgentRunRail, type AgentRailStep } from "./AgentRunRail";
import { classifyReadFailure, copyFor, type Attribution } from "../../lib/failureAttribution";


type Message = { role: "user" | "assistant"; text: string; response?: CopilotResponse };
type NativeRun = NonNullable<CopilotResponse["agent_run"]>;

const EVENT_TYPES = [
  "semantic.interpreted",
  "mission.planned",
  "run.started",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "gateway.checked",
  "guardian.checked",
  "proposal.created",
  "approval.required",
  "run.completed",
  "run.failed",
  "run.stopped",
] as const;

const TERMINAL_RE = /^(completed|succeeded|failed|cancelled|canceled|stopped|error)$/i;

const lifecycleLabels: Record<string, { en: string; zh: string }> = {
  "semantic.interpreted": { en: "Understanding request", zh: "理解请求" },
  "mission.planned": { en: "Steps prepared", zh: "已准备好步骤" },
  "run.started": { en: "Started working", zh: "开始处理" },
  "gateway.checked": { en: "Checked against your trip rules", zh: "已按你的行程规则核对" },
  "guardian.checked": { en: "Checked your protected plans", zh: "已核对你的受保护安排" },
  "proposal.created": { en: "Proposal created", zh: "方案已生成" },
  "approval.required": { en: "Waiting for approval", zh: "等待确认" },
  "run.completed": { en: "Finished", zh: "已处理完成" },
  "run.failed": { en: "Could not finish", zh: "暂时无法完成" },
  "run.stopped": { en: "Stopped", zh: "已停止" },
};

const toolLabels: Record<string, { en: string; zh: string }> = {
  inspect_journey: { en: "Inspect current Journey", zh: "检查当前行程" },
  inspect_twin: { en: "Check my trip facts", zh: "核对我的行程事实" },
  list_replans: { en: "Inspect proposals and commitments", zh: "检查方案和受保护承诺" },
  search_city_services: { en: "Search candidate places and services", zh: "搜索候选地点和服务" },
  request_replan: { en: "Ask for a revised plan", zh: "请求调整方案" },
  accept_replan: { en: "Accept replan", zh: "接受重规划" },
  replace_activity: { en: "Replace activity", zh: "替换活动" },
  reschedule_activity: { en: "Reschedule activity", zh: "调整活动时间" },
  create_booking: { en: "Create booking", zh: "创建预订" },
  cancel_refundable_booking: { en: "Cancel refundable booking", zh: "取消可退款预订" },
  request_commitment_change: { en: "Request commitment change", zh: "请求修改受保护承诺" },
};

function terminalStatus(value: string | null | undefined) {
  return TERMINAL_RE.test(String(value ?? ""));
}

function eventKey(event: NativeRuntimeEvent) {
  return [
    event.event_type,
    event.event_id ?? "",
    event.sequence ?? "",
    event.tool_name ?? "",
    event.timestamp ?? "",
    event.duration_ms ?? "",
    event.approval_id ?? "",
    event.proposal_id ?? "",
  ].join("|");
}

function readStoredEvents(journeyId: string, runId: string): NativeRuntimeEvent[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(`citynexus.native-hermes.timeline.${journeyId}.${runId}`);
    const value = raw ? JSON.parse(raw) : [];
    return Array.isArray(value) ? (value as NativeRuntimeEvent[]).slice(-250) : [];
  } catch {
    return [];
  }
}

function mergeEvents(...groups: Array<NativeRuntimeEvent[] | undefined>) {
  const result: NativeRuntimeEvent[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const item of group ?? []) {
      if (!item || typeof item !== "object") continue;
      const key = eventKey(item);
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(item);
    }
  }
  return result
    .map((event, index) => ({ event, index }))
    .sort((left, right) => {
      const leftSequence = Number.isFinite(left.event.sequence) ? Number(left.event.sequence) : null;
      const rightSequence = Number.isFinite(right.event.sequence) ? Number(right.event.sequence) : null;
      if (leftSequence != null && rightSequence != null && leftSequence !== rightSequence) {
        return leftSequence - rightSequence;
      }
      const leftTime = left.event.timestamp ? Date.parse(String(left.event.timestamp)) : Number.NaN;
      const rightTime = right.event.timestamp ? Date.parse(String(right.event.timestamp)) : Number.NaN;
      if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
        return leftTime - rightTime;
      }
      return left.index - right.index;
    })
    .map(({ event }) => event)
    .slice(-250);
}

function statusForEvent(event: NativeRuntimeEvent): AgentRailStep["status"] {
  if (event.event_type === "approval.required" || event.status === "approval_required") return "approval";
  if (event.event_type.endsWith(".failed") || event.status === "failed") return "failed";
  if (event.event_type.endsWith(".completed") || event.status === "completed") return "done";
  if (event.event_type === "run.stopped" || event.status === "stopped") return "failed";
  return "active";
}

function formatDuration(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function summaryText(event: NativeRuntimeEvent, zh: boolean) {
  if (event.tool_name && toolLabels[event.tool_name]) {
    return toolLabels[event.tool_name][zh ? "zh" : "en"];
  }
  return lifecycleLabels[event.event_type]?.[zh ? "zh" : "en"] ?? event.friendly_label;
}

function safeJson(value: unknown) {
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function NativeMissionTimeline({
  data,
  run,
  onNavigate,
  compact = false,
  onExecutionChange,
}: {
  data: V8Data;
  run: NativeRun;
  onNavigate: (route: string) => void;
  compact?: boolean;
  onExecutionChange?: (executed: boolean) => void;
}) {
  const { locale } = useLocale();
  const t = useT();
  const zh = locale === "zh-CN";
  const [events, setEvents] = useState<NativeRuntimeEvent[]>(() =>
    mergeEvents(run.initial_events, readStoredEvents(data.journey.id, run.run_id)),
  );
  const [runState, setRunState] = useState(run.status);
  const [actionState, setActionState] = useState<string | null>(null);
  const [actionFacts, setActionFacts] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);

  const appendEvents = useCallback((incoming: NativeRuntimeEvent[]) => {
    setEvents((current) => mergeEvents(current, incoming));
  }, []);

  // A closed panel or route unmount does not discard the mission timeline.
  // This is only a local UI cache; authoritative history is the server audit
  // timeline fetched below.
  useEffect(() => {
    setEvents(mergeEvents(run.initial_events, readStoredEvents(data.journey.id, run.run_id)));
    setRunState(run.status);
    setActionState(null);
    setStreamError(null);
  }, [data.journey.id, run.run_id]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        `citynexus.native-hermes.timeline.${data.journey.id}.${run.run_id}`,
        JSON.stringify(events.slice(-250)),
      );
    } catch {
      // Local storage is an enhancement only; server audit history remains authoritative.
    }
  }, [data.journey.id, events, run.run_id]);

  useEffect(() => {
    let cancelled = false;
    void cityNexusApi
      .getNativeHermesTimeline(data.journey.id, run.run_id)
      .then((payload) => {
        if (!cancelled) appendEvents(payload.events);
      })
      .catch(() => {
        // A freshly-created run may not have emitted an event yet.
      });
    return () => {
      cancelled = true;
    };
  }, [appendEvents, data.journey.id, run.run_id]);

  useEffect(() => {
    const url = run.events_url;
    if (!url || typeof window === "undefined") return undefined;
    let cancelled = false;
    let ended = false;
    let attempts = 0;
    let retryTimer: number | undefined;
    let source: EventSource | null = null;
    const connect = () => {
      if (cancelled || ended) return;
      source = new EventSource(url);
      const handleEvent = (incoming: Event) => {
        try {
          const payload = JSON.parse((incoming as MessageEvent<string>).data) as NativeRuntimeEvent;
          if (!payload || typeof payload !== "object") return;
          attempts = 0;
          setStreamError(null);
          appendEvents([payload]);
          if (payload.status) setRunState(String(payload.status));
          if (["run.completed", "run.failed", "run.stopped"].includes(payload.event_type)) {
            ended = true;
            source?.close();
            source = null;
          }
        } catch {
          setStreamError(zh ? "运行事件暂时无法解析" : "A runtime event could not be read");
        }
      };
      source.addEventListener("message", handleEvent);
      for (const name of EVENT_TYPES) source.addEventListener(name, handleEvent);
      source.onerror = () => {
        if (ended || cancelled) return;
        source?.close();
        source = null;
        attempts += 1;
        if (attempts <= 3) {
          setStreamError(zh ? "正在重新连接运行事件" : "Reconnecting to runtime events");
          retryTimer = window.setTimeout(connect, Math.min(5000, 500 * 2 ** (attempts - 1)));
        } else {
          setStreamError(zh ? "运行事件流不可用；已保留已收到的事件" : "Event stream unavailable; received events are preserved");
        }
      };
    };
    connect();
    return () => {
      cancelled = true;
      ended = true;
      if (retryTimer != null) window.clearTimeout(retryTimer);
      source?.close();
    };
  }, [appendEvents, run.events_url, zh]);

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      void cityNexusApi
        .getNativeHermesRun(data.journey.id, run.run_id)
        .then((payload) => {
          if (!cancelled && typeof payload.status === "string") setRunState(payload.status);
        })
        .catch(() => undefined);
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [data.journey.id, run.run_id]);

  const executed = useMemo(
    () => events.some((event) => event.source === "HERMES_NATIVE" && (
      event.event_type === "run.started" || event.event_type.startsWith("tool.")
    )),
    [events],
  );
  useEffect(() => onExecutionChange?.(executed), [executed, onExecutionChange]);

  const steps = useMemo<AgentRailStep[]>(() => events.map((event, index) => {
    // The default rail must never print a raw MCP tool name (snake_case
    // engineering vocabulary).  Show the human label for the tool, or a human
    // status when the tool is unmapped.  The raw name stays in Advanced.
    const humanStep = event.tool_name
      ? (toolLabels[event.tool_name]?.[zh ? "zh" : "en"] ?? humanAgentStatus(event.status, zh))
      : humanAgentStatus(event.status, zh);
    const runMeta: ReactNode = (
      /* The number on screen stays this screen's own formatting, but the element carrying it says
         which allowed field it measured, under the contract name (Q70) -- so a gauge binds the
         field and not the words around it.  No duration means no anchor, not a zero. */
      <span
        className="cn-agent-event-detail"
        {...(event.duration_ms == null
          ? {}
          : {
              "data-cnx-field": "elapsed_ms",
              "data-cnx-value": String(Number(event.duration_ms)),
            })}
      >
        {humanStep}
        {event.duration_ms != null && ` · ${formatDuration(Number(event.duration_ms))}`}
        {""}
      </span>
    );
    return {
      id: `${index}-${eventKey(event)}`,
      label: summaryText(event, zh),
      status: statusForEvent(event),
      /* `result_summary` is left out on purpose: the server builds it from whatever a tool
         returned, so it can carry a sentence this screen cannot vouch for.  Q64 lets the allowed
         fields through, not the answer's prose -- and Q71 says a collapsed block is still in the
         DOM. */
      runMeta: (
        <>
          {runMeta}
          <details className="cn-agent-raw">
            <summary>{zh ? "运行详情" : "Runtime details"}</summary>
            <code>{safeJson({
              event_type: event.raw_event_type ?? event.event_type,
              source: event.source,
              policy_decision: event.policy_decision,
            })}</code>
          </details>
        </>
      ),
    };
  }), [events, zh]);

  const toolEvents = events.filter((event) => event.event_type.startsWith("tool."));
  const currentTool = [...events].reverse().find((event) => event.event_type === "tool.started")?.tool_name;
  const totalDuration = events.reduce((sum, event) => sum + (Number(event.duration_ms) || 0), 0);
  const approvalEvent = [...events].reverse().find((event) =>
    event.event_type === "approval.required" || (
      Boolean(event.approval_id) && String(event.approval_status ?? "PENDING").toUpperCase() === "PENDING"
    ),
  );
  const proposalEvent = [...events].reverse().find((event) => event.event_type === "proposal.created" || event.proposal_id);
  const active = !terminalStatus(runState) && !events.some((event) => event.event_type === "run.stopped");

  const stop = async () => {
    if (!active) return;
    setActionState(zh ? "正在停止…" : "Stopping…");
    try {
      await cityNexusApi.stopNativeHermesRun(data.journey.id, run.run_id);
      setRunState("stopped");
      setActionState(zh ? "已通过 CityNexus 停止" : "Stopped through CityNexus");
    } catch (cause) {
      const attribution: Attribution = classifyReadFailure(cause);
      setActionFacts(attribution.telemetry);
      setActionState(copyFor(attribution, zh).body ?? t("agent.stopFailed"));
    }
  };

  const approve = async () => {
    if (typeof navigator !== "undefined" && !navigator.onLine) {
      setActionState(zh ? "离线时不能确认方案" : "Approval is disabled while offline");
      return;
    }
    if (!approvalEvent?.approval_id) {
      setActionState(zh ? "缺少 CityNexus approval_id，需打开恢复中心审核" : "CityNexus approval_id is not available; review in Recovery");
      return;
    }
    setActionState(zh ? "正在记录 CityNexus 确认…" : "Recording CityNexus approval…");
    try {
      await cityNexusApi.approveAgentAction(data.journey.id, approvalEvent.approval_id);
      setActionState(zh ? "确认已记录；仍需由 CityNexus 执行授权" : "Approval recorded; CityNexus still owns execution authorization");
    } catch (cause) {
      const attribution: Attribution = classifyReadFailure(cause);
      setActionFacts(attribution.telemetry);
      setActionState(copyFor(attribution, zh).body ?? t("agent.approvalFailed"));
    }
  };

  return (
    <section className={`cn-native-mission ${compact ? "compact" : ""}`} data-testid="native-hermes-timeline">
      <header className="cn-native-mission-head">
        <div>
          <b>{zh ? "正在为你处理" : "Working on your request"}</b>
          <small>
            {executed
              ? zh ? "已按你的行程事实执行" : "Ran against your journey facts"
              : zh ? "正在核对你的行程事实…" : "Checking your journey facts…"}
          </small>
        </div>
      </header>

      {steps.length ? (
        <AgentRunRail steps={steps} />
      ) : (
        <p className="cn-native-empty">
          {zh ? "正在核对你的行程事实…" : "Checking your journey facts…"}
        </p>
      )}

      <details className="cn-native-plan" data-testid="native-hermes-observability">
        <summary>{zh ? "高级详情" : "Advanced detail"}</summary>
        <div className="cn-copilot-observability">
          <span><b>{zh ? "任务编号" : "task id"}</b> <code>{run.run_id}</code></span>
          {run.mission_id && <span><b>{zh ? "内部任务" : "mission"}</b> <code>{run.mission_id}</code></span>}
          <span><b>{zh ? "已用步骤" : "steps"}</b> {toolEvents.length}</span>
          <span><b>{zh ? "当前" : "current"}</b> {currentTool ? (toolLabels[currentTool]?.[zh ? "zh" : "en"] ?? t("agent.toolWorking")) : "—"}</span>
          {currentTool && (
            <span><b>{zh ? "工具" : "tool"}</b> <code>{currentTool}</code></span>
          )}
          <span><b>{zh ? "耗时" : "duration"}</b> {formatDuration(totalDuration)}</span>
          <span><b>{zh ? "执行方式" : "execution"}</b> {lastRuntimeLabel(executed, zh)}</span>
        </div>
      </details>

      {run.mission_plan && (
        <details className="cn-native-plan">
          <summary>{zh ? "查看安全任务计划" : "View safe mission plan"}</summary>
          <ol>
            {run.mission_plan.steps.map((step) => (
              <li key={step.id}>
                <span>{zh ? localizePlanStep(step.operation) : step.operation}</span>
                {step.requires_authority && <small>{zh ? "CityNexus 授权" : "CityNexus authority"}</small>}
              </li>
            ))}
          </ol>
        </details>
      )}

      <div className="cn-native-proposal">
        <b>{zh ? "建议与受保护安排" : "Suggestions & protected plans"}</b>
        <span>
          {proposalEvent
            ? zh ? "已经生成建议，展开高级详情可以看到过程。" : "A suggestion is ready; open advanced detail to see how it was produced."
            : zh ? "还没有需要你决定的改动。" : "There is nothing for you to decide yet."}
        </span>
        <div className="cn-protected-badges">
          {(run.protected_commitments ?? []).map((commitment, index) => (
            <span key={String(commitment.id ?? index)}>{zh ? "受保护承诺" : "Protected commitment"}{commitment.id ? ` · ${String(commitment.id).slice(0, 8)}` : ""}</span>
          ))}
          {!run.protected_commitments?.length && run.mission_plan?.protected_activity_ids.map((id) => (
            <span key={id}>{zh ? "受保护活动" : "Protected activity"} · {id.slice(0, 8)}</span>
          ))}
        </div>
      </div>

      {approvalEvent && (
        <div className="cn-copilot-approval" data-testid="native-hermes-approval">
          <b>{zh ? "需要你确认" : "Needs your confirmation"}</b>
          <span>
            {zh
              ? "任何会影响已确认预订或受保护安排的改动，都需要你先确认，系统不会自己改。"
              : "Anything that affects a confirmed booking or a protected plan needs your confirmation first; nothing is changed automatically."}
          </span>
          <div>
            <button type="button" className="v8-primary" onClick={() => void approve()}>{zh ? "批准" : "Approve"}</button>
            <button type="button" className="v8-secondary" onClick={() => void stop()}>{zh ? "拒绝" : "Reject"}</button>
            <button type="button" className="v8-outline-button" onClick={() => onNavigate("authoritative")}>{zh ? "修改 / 审核" : "Modify / Review"}</button>
          </div>
        </div>
      )}

      {actionState && (
        <p className="cn-copilot-action-state" role="status" data-testid="copilot-action-state"
           data-attribution={actionFacts ?? ""}>{actionState}</p>
      )}
      {streamError && <p className="cn-copilot-stream-state" role="status">{streamError}</p>}
      {active && <button type="button" className="cn-copilot-stop" onClick={() => void stop()}>{zh ? "停止任务" : "Stop mission"}</button>}
    </section>
  );
}

function localizePlanStep(operation: string) {
  const values: Record<string, string> = {
    "Inspect current Journey": "检查当前行程",
    "Inspect JourneyTwin": "核对我的行程事实",
    "Inspect protected commitments": "检查受保护承诺",
    "Inspect affected activities": "检查受影响活动",
    "Search candidate places and services": "搜索候选地点和服务",
    "Evaluate candidates against constraints": "按约束评估候选项",
    "Request a deterministic replan proposal": "请求调整方案",
    "Inspect proposal trade-offs": "检查方案权衡",
    "Proposal only; no mutation without user approval": "仅方案；未经确认不修改",
  };
  return values[operation] ?? operation;
}

// Traveller-facing labels.  Intent enums, verification states and action
// identifiers are internal routing vocabulary and must never reach the
// traveller as raw SCREAMING_CASE strings.
const INTENT_LABELS: Record<string, { zh: string; en: string }> = {
  EXPLAIN_PLAN: { zh: "解释当前安排", en: "Explains your plan" },
  WHAT_IF: { zh: "试算变化", en: "Previews a change" },
  VERIFY_FACT: { zh: "核对信息", en: "Checks a claim" },
  DISCOVER: { zh: "查找地点", en: "Finds places" },
  PREFERENCE_CHANGE: { zh: "偏好更新", en: "Preference update" },
  DISRUPTION_REPORT: { zh: "报告中断", en: "Reports a disruption" },
  REPLAN_REQUEST: { zh: "重新规划", en: "Replan request" },
  PERSONAL_ASSISTANCE: { zh: "个人求助", en: "Personal help" },
  UNKNOWN: { zh: "其他问题", en: "Other" },
};

const VERIFICATION_LABELS: Record<string, { zh: string; en: string }> = {
  VERIFIED: { zh: "已核验", en: "Verified" },
  PARTIALLY_VERIFIED: { zh: "部分核验", en: "Partly verified" },
  UNVERIFIED: { zh: "未核验", en: "Unverified" },
  FAILED_EXTERNAL: { zh: "暂时无法核验", en: "Could not verify" },
  NOT_CONFIGURED: { zh: "暂不支持核验", en: "Verification unavailable" },
};

const ACTION_LABELS: Record<string, { zh: string; en: string }> = {
  REQUEST_REPLAN: { zh: "查看可选调整", en: "See possible changes" },
  REQUEST_PREFERENCE_UPDATE: { zh: "更新我的偏好", en: "Update my preferences" },
  REQUEST_PLAN_EXPLANATION: { zh: "查看安排理由", en: "See why" },
  REQUEST_PLACE_SEARCH: { zh: "搜索更多地点", en: "Search more places" },
  VERIFY_EVIDENCE: { zh: "查看来源", en: "See sources" },
  REVIEW_FUTURE_EVENT_TARGET: { zh: "确认受影响安排", en: "Confirm what is affected" },
};

// Agent lifecycle statuses are internal enum values.  A traveller reads a
// plain sentence; the raw value only appears under Advanced.
function humanAgentStatus(value: string | null | undefined, zh: boolean): string {
  const key = String(value ?? "").toUpperCase();
  const map: Record<string, { zh: string; en: string }> = {
    RUNNING: { zh: "正在处理", en: "Working" },
    STARTED: { zh: "已开始", en: "Started" },
    PENDING: { zh: "等待中", en: "Waiting" },
    COMPLETED: { zh: "已完成", en: "Done" },
    SUCCEEDED: { zh: "已完成", en: "Done" },
    FAILED: { zh: "暂时无法完成", en: "Could not finish" },
    FAILED_EXTERNAL: { zh: "服务暂时不可用", en: "Service unavailable" },
    STOPPED: { zh: "已停止", en: "Stopped" },
    CANCELLED: { zh: "已取消", en: "Cancelled" },
    APPROVAL_REQUIRED: { zh: "等待你确认", en: "Needs your confirmation" },
  };
  const entry = map[key];
  if (entry) return zh ? entry.zh : entry.en;
  if (!key) return zh ? "处理中" : "Working";
  return zh ? "处理中" : "Working";
}

function lastRuntimeLabel(executed: boolean, zh: boolean): string {
  return executed
    ? zh ? "已按行程事实执行" : "Executed against journey facts"
    : zh ? "仅在本机规划" : "Planned locally only";
}

function humanIntent(value: string, zh: boolean): string {
  const entry = INTENT_LABELS[String(value).toUpperCase()];
  return entry ? (zh ? entry.zh : entry.en) : (zh ? "问题" : "Question");
}

function humanVerification(value: string, zh: boolean): string {
  const entry = VERIFICATION_LABELS[String(value).toUpperCase()];
  return entry ? (zh ? entry.zh : entry.en) : (zh ? "核验状态未知" : "Verification unknown");
}

function humanAction(value: string, zh: boolean): string {
  const entry = ACTION_LABELS[String(value).toUpperCase()];
  return entry ? (zh ? entry.zh : entry.en) : (zh ? "继续" : "Continue");
}

type Band = { left: number; top: number; right: number; bottom: number };

export function CopilotDock({
  data,
  onNavigate,
}: {
  data: V8Data;
  onNavigate: (route: string) => void;
}) {
  const t = useT();
  const { locale } = useLocale();
  const zh = locale === "zh-CN";
  const [open, setOpen] = useState(false);
  // R6: the dock must never cover a sheet, a drawer, or a Live proposal.  The
  // selectors are the real high-priority surfaces; while any is present the
  // dock collapses to its edge button and restores itself afterwards.
  const [yieldToSurface, setYieldToSurface] = useState(false);
  useEffect(() => {
    if (typeof document === "undefined") return;
    const HIGH_PRIORITY = [
      '[data-testid="plan-ai-sheet"]',
      ".v8-sheet-backdrop",
      ".v8-drawer-backdrop",
      '[data-testid="live-proposal"]',
    ].join(",");
    const sync = () => setYieldToSurface(Boolean(document.querySelector(HIGH_PRIORITY)));
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true });
    return () => observer.disconnect();
  }, []);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [messages, setMessages] = useState<Message[]>(() => readStoredMessages(data.journey.id));
  const [error, setError] = useState<string | null>(null);
  const [sendFacts, setSendFacts] = useState<string | null>(null);
  // Whether execution was observed against the Journey.  The internal
  // runtime identity never reaches the traveller.
  const [executed, setExecuted] = useState(false);
  const latestNativeRun = useMemo<NativeRun | null>(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const run = messages[index].response?.agent_run;
      if (run) return run;
    }
    return null;
  }, [messages]);
  const localeCode = locale === "zh-CN" ? "zh" : "en";

  useEffect(() => {
    try {
      window.localStorage.setItem(
        `citynexus.copilot.history.${data.journey.id}`,
        JSON.stringify(messages.slice(-50)),
      );
    } catch {
      // Local persistence is best effort.
    }
  }, [data.journey.id, messages]);

  const send = async () => {
    const value = draft.trim();
    if (value.length < 2 || sending) return;
    setSending(true);
    setError(null);
    setDraft("");
    setMessages((current) => [...current, { role: "user", text: value }]);
    try {
      const response = await cityNexusApi.sendCopilotMessage(data.journey.id, value, localeCode);
      // Until a real lifecycle/tool event arrives, nothing is claimed as
      // executed; the timeline callback upgrades this only on real evidence.
      setExecuted(false);
      setMessages((current) => [...current, { role: "assistant", text: response.message, response }]);
    } catch (cause) {
      const attribution: Attribution = classifyReadFailure(cause);
      setSendFacts(attribution.telemetry);
      setError(copyFor(attribution, zh).body ?? t("agent.sendError"));
    } finally {
      setSending(false);
    }
  };

  const continueAction = (action?: string) => {
    if (action === "REQUEST_PLACE_SEARCH") onNavigate("discover");
    if (action === "REQUEST_REPLAN") onNavigate("authoritative");
    if (action === "REQUEST_PREFERENCE_UPDATE") onNavigate("me");
  };

  // R6: while a sheet, drawer or Live proposal is open the dock collapses to
  // its edge button instead of covering the primary action.  The traveller's
  // own open/closed choice is remembered and restored when the surface closes.
  const expanded = open && !yieldToSurface;
  /* Pass-9 blocker #2: the traveller already gets a Copilot entry in the bottom
     navigation, so a full-width persistent bar floating over Today's content
     meant two competing invitations and a screen where the dock outranked the
     journey. The L2 decision state proved a compact pill is enough, so idle
     (nothing open, nothing running) reuses exactly that presentation, and the
     bar returns for an active conversation or a task worth interrupting for. */
  const idleCompact = !open && !sending && !yieldToSurface;

  /* Pass-13.1: the pill is `position: fixed`, so wherever the page happens to
     come to rest, that is where the pill sits on top of it.  The bottom
     navigation gets away with this because the shell reserves a band for it;
     the pill floats in the middle of the content column, and pass-13 measured it
     resting over the DAY 1 card (158x54 of a tappable surface) at a settled
     scroll position.  Reserving that band properly would cost a phone user
     ~138px of every screen, so instead the pill yields: once scrolling settles, if
     text the traveller is reading or a control they can use lies under it, it
     collapses to a corner cell in the nearest free slot and stays tappable there.
     Nothing is lost — the bottom navigation still carries a Copilot entry, and the
     pill itself never leaves the viewport. */
  const dockRef = useRef<HTMLElement | null>(null);
  const [restOverlap, setRestOverlap] = useState(false);
  const [yieldSlot, setYieldSlot] = useState<{ left: number; top: number } | null>(null);
  /* The band the pill rests in, remembered from a frame when it was at rest.  Once it has
     relocated, offsetLeft/offsetTop report the relocated box, and a trigger that reads its own
     output flips back every frame: the pill would slide in and out forever. */
  const restBandRef = useRef<{ left: number; top: number; right: number; bottom: number } | null>(null);
  const yieldSlotRef = useRef<{ left: number; top: number } | null>(null);

  useEffect(() => {
    if (!idleCompact) {
      restBandRef.current = null;
      yieldSlotRef.current = null;
      setRestOverlap(false);
      setYieldSlot(null);
      return;
    }
    const READABLE =
      "h1,h2,h3,h4,h5,h6,p,li,dt,dd,th,td,label,figcaption,caption,summary,blockquote,small,strong,em,time,code";
    const ACTIONABLE = 'button, a[href], input, select, textarea, [role="button"]';
    const SLOT = 44;
    const INSET = 10;
    const band = (r: DOMRect): Band => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom });
    const overlaps = (a: Band, b: Band) =>
      Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
      Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
    const measure = () => {
      const node = dockRef.current;
      if (!node) return;
      const vh = window.innerHeight;
      const vw = window.innerWidth;
      /* The resting band, not the current painted rectangle: while the pill sits at rest its
         layout box is where it rests.  Once it has relocated, its own layout box has moved too
         -- relocating changes left/top, not just a transform -- so the band is remembered from
         the last frame at rest.  A trigger that measured its own output would flip back every
         frame and the pill would slide in and out forever. */
      if (yieldSlotRef.current && restBandRef.current) {
        // at rest elsewhere; keep the remembered band
      } else {
        const live = node.getBoundingClientRect();
        restBandRef.current = { left: live.left, top: live.top, right: live.right, bottom: live.bottom };
      }
      const rest: Band = restBandRef.current;
      /* Whether the pill is *over text* is decided on painted glyphs (Range.getClientRects),
         because a block-level heading spans the whole content column and its empty line-end is
         not something the traveller is reading.  Where the collapsed cell may be placed is
         decided against both the glyph rects and the element boxes, so a cell in a vertical gap
         between lines is preferred over one that merely touches no glyph. */
      const ink: Band[] = [];
      const blocks: Band[] = [];
      document.querySelectorAll(READABLE).forEach((el) => {
        if (node.contains(el) || el.contains(node)) return;
        const own = el.getBoundingClientRect();
        if (own.width > 0 && own.height > 0 && own.bottom > 0 && own.top < vh) blocks.push(band(own));
        const range = document.createRange();
        range.selectNodeContents(el);
        Array.from(range.getClientRects()).forEach((r) => {
          if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < vh) ink.push(band(r));
        });
      });
      const controls: Band[] = [];
      document.querySelectorAll(ACTIONABLE).forEach((el) => {
        if (node.contains(el) || el.contains(node)) return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0 || r.bottom < 0 || r.top > vh) return;
        controls.push(band(r));
      });
      const bar = document.querySelector(".v8-mobile-nav");
      const barTop = bar ? Math.max(INSET, bar.getBoundingClientRect().top) : vh;
      /* The band is considered occupied when it sits on painted glyphs, on a text element's own
         box, or on another control.  The box is in there because a 165x54 bar lying across the
         tail of a line reads as "covering that line" to the traveller even where the last glyph
         stops short of it -- and because a free cell exists at every measured rest, so accepting
         the box as an obstacle costs nothing but the pill's usual corner. */
      const covered =
        ink.some((b) => overlaps(b, rest)) ||
        blocks.some((b) => overlaps(b, rest)) ||
        controls.some((b) => overlaps(b, rest));
      setRestOverlap(covered);
      if (!covered) {
        yieldSlotRef.current = null;
        setYieldSlot(null);
        return;
      }
      /* Yielding relocates.  A block of text spans the content column, so the free place to put
         the cell is a vertical gap, not a horizontal corner: sweep the two edge columns downward
         from just above the reserved bar in 10 px steps and take the first cell that sits on no
         painted glyph, inside no text element's own box, and on no other control.  If nothing is
         free the pill stays where it is and keeps answering taps -- a hidden control is not a
         yielded one. */
      const ceiling = Math.max(INSET, Math.min(barTop - INSET - SLOT, vh - INSET - SLOT));
      const candidates: Band[] = [];
      for (const left of [vw - INSET - SLOT, INSET]) {
        for (let top = ceiling; top >= INSET; top -= 10) {
          candidates.push({ left, top, right: left + SLOT, bottom: top + SLOT });
        }
      }
      /* 6 px of slack: a cell whose edge merely touches a glyph run or a text box is not counted free. */
      const pad = (c: Band, n: number): Band => ({
        left: c.left - n, top: c.top - n, right: c.right + n, bottom: c.bottom + n,
      });
      const clear = (c: Band) =>
        !ink.some((b) => overlaps(b, pad(c, 6))) &&
        !blocks.some((b) => overlaps(b, pad(c, 6))) &&
        !controls.some((b) => overlaps(b, c));
      const free = candidates.find(clear);
      const loose = candidates.find(
        (c) => !ink.some((b) => overlaps(b, pad(c, 6))) && !controls.some((b) => overlaps(b, c)),
      );
      const cell = free || loose || candidates[0] || rest;
      const slot = { left: Math.round(cell.left), top: Math.round(cell.top) };
      yieldSlotRef.current = slot;
      setYieldSlot(slot);
    };
    /* Measure once the scroll has settled, not during it: while the page is
       moving, content passing under a floating accessory is not the defect. */
    let timer = 0;
    /* A settled scroll is not one stable frame: CJK fallback and the panels' entrance
       animation can move a line of text after the first measurement. Two bounded
       re-checks follow each settle, and the counter resets on the next scroll, so this
       is a second look, not a poll. */
    let passes = 0;
    const measureSettled = () => {
      measure();
      if (passes < 2) {
        passes += 1;
        window.setTimeout(measureSettled, 320);
      }
    };
    const schedule = () => {
      window.clearTimeout(timer);
      passes = 0;
      timer = window.setTimeout(measureSettled, 220);
    };
    /* A resize changes where the pill rests, so forget the remembered band and let the next
       at-rest measurement re-derive it; the pill returns to rest for that one frame. */
    const onResize = () => {
      restBandRef.current = null;
      yieldSlotRef.current = null;
      setYieldSlot(null);
      schedule();
    };
    /* A hub card list grows after the scenario arrives, and that reflow moves text under a pill
       that never received a scroll event. Watching the body's box is the cheapest way to learn the
       layout changed; the debounce and the two bounded re-checks are the same ones scrolling uses. */
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    ro?.observe(document.body);
    measureSettled();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", onResize);
    return () => {
      window.clearTimeout(timer);
      ro?.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", onResize);
    };
  }, [idleCompact]);

  const yieldsAtRest = idleCompact && restOverlap;

  return (
    <aside
      ref={dockRef}
      className={`cn-copilot-dock ${expanded ? "open" : "collapsed"}${yieldToSurface ? " yield-to-surface" : ""}${idleCompact ? " idle-compact" : ""}${yieldsAtRest ? " yield-at-rest" : ""}`}
      data-testid="v18-copilot-dock"
      data-yield-to-surface={yieldToSurface ? "true" : "false"}
      data-idle-compact={idleCompact ? "true" : "false"}
      data-rest-overlap={yieldsAtRest ? "true" : "false"}
      data-yield-slot={
        yieldsAtRest && yieldSlot ? `${yieldSlot.left},${yieldSlot.top},44` : "none"
      }
      style={
        yieldsAtRest && yieldSlot
          ? ({
              "--cn-yield-left": `${yieldSlot.left}px`,
              "--cn-yield-top": `${yieldSlot.top}px`,
            } as CSSProperties)
          : undefined
      }
    >
      {!expanded ? (
        <button
          className="cn-copilot-edge"
          type="button"
          onClick={() => setOpen(true)}
          aria-label={t("agent.citynexus")}
        >
          <span className="cn-copilot-edge-mark">✦</span>
          <span>{t("agent.citynexus")}</span>
          <i className="cn-copilot-status" />
        </button>
      ) : (
        <div className="cn-copilot-panel">
          <header className="cn-copilot-head">
            <div>
              <b>{t("agent.citynexus")}</b>
              <small>
                {locale === "zh-CN"
                  ? "回答只依据已核验的行程事实，不会自己改动你的安排。"
                  : "Answers use verified journey facts only and never change your plan."}
              </small>
            </div>
            <button type="button" className="cn-copilot-close" onClick={() => setOpen(false)} aria-label={t("shell.close")}>×</button>
          </header>
          <div className="cn-copilot-history" aria-live="polite">
            {messages.length === 0 && <p className="cn-copilot-empty">{locale === "zh-CN" ? "问我为什么这样安排、试算变化，或报告已经确认的中断。" : "Ask why, preview a change, or report a confirmed disruption."}</p>}
            {messages.map((item, index) => (
              <article key={`${item.role}-${index}`} className={`cn-copilot-bubble ${item.role}`}>
                <small>{item.role === "user" ? (locale === "zh-CN" ? "你" : "You") : "CityNexus"}</small>
                <p>{item.text}</p>
                {item.response?.intent && (
                  <span className="cn-copilot-chip">{humanIntent(item.response.intent.name, zh)}</span>
                )}
                {item.response?.verification && (
                  <span className="cn-copilot-chip">{humanVerification(item.response.verification.status, zh)}</span>
                )}
                {item.response?.action?.type && (
                  <button
                    type="button"
                    className="cn-copilot-link"
                    onClick={() => continueAction(item.response?.action?.type)}
                  >
                    {humanAction(item.response.action.type, zh)}
                  </button>
                )}
              </article>
            ))}
          </div>
          {latestNativeRun && (
            <NativeMissionTimeline
              data={data}
              run={latestNativeRun}
              onNavigate={onNavigate}
              onExecutionChange={setExecuted}
            />
          )}
          {error && (
            <p className="cn-copilot-error" role="alert" data-testid="copilot-send-error"
               data-attribution={sendFacts ?? ""}>{error}</p>
          )}
          <form className="cn-copilot-form" onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={t("agent.inputPlaceholder")} aria-label={t("agent.message")} rows={2} />
            <button className="v8-primary" type="submit" disabled={sending || draft.trim().length < 2}>{sending ? t("agent.working") : t("agent.send")}</button>
          </form>
        </div>
      )}
    </aside>
  );
}

function readStoredMessages(journeyId: string): Message[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(`citynexus.copilot.history.${journeyId}`);
    const value = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is Message =>
      item && (item.role === "user" || item.role === "assistant") && typeof item.text === "string",
    ).slice(-50);
  } catch {
    return [];
  }
}
