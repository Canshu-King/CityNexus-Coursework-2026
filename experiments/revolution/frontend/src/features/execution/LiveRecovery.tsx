/**
 * Live journey + governed replan (reference 10).
 *
 * Traveller language only:
 *   发生了什么 / 哪些受影响 / 哪些不变 / 推荐怎么改 / 接受 / 仅调整今天 / 稍后提醒
 *
 * Internal vocabulary (Event, ReplanProposal, C4, PlanVersion, authority
 * envelope) never reaches this surface.  Accepting calls the governed accept
 * endpoint; nothing mutates the plan locally.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CityNexusApiError,
  cityNexusApi,
  type InTransitRerouteResult,
  type GroupRecoveryPlanResult,
  type GroupRendezvousRouteResult,
  type JourneyOperatorProjection,
} from "../../api/citynexus";
import { travelerAuthHeaders } from "../../lib/travelerSession";
import { useLocale, useT } from "../../i18n/LocaleContext";
import {
  isDisruptionAcknowledgedOnly,
  isDisruptionOpen,
} from "../../lib/disruptionState";
import {
  fetchWithWriteDeadline,
  isWriteDeadlineMissed,
  writeDeadlineCopy,
} from "../../lib/writeDeadline";
import {
  useAuthoritativePlans,
  useAuthoritativeTwin,
} from "../../api/v12Authoritative";
import { MemberSideQuestCard } from "./MemberSideQuestCard";
import { MemberMeetupHandshakePanel } from "./MemberMeetupHandshakePanel";
import { MemberPlaceInvitationPanel } from "./MemberPlaceInvitationPanel";
import { MomentScoutCard } from "./MomentScoutCard";

type Proposal = {
  id: string;
  status: string;
  recovery_epoch_id?: string | null;
  source_agent_run_id?: string | null;
  hard_constraints_passed: boolean;
  locked_commitments_preserved: boolean;
  diff: { removed?: string[]; added?: string[]; moved?: string[] };
  explanation?: { impact_model?: string; summary?: string };
};

type GuardianPulse = {
  status?: string;
  completed_at?: string | null;
  result?: {
    signals?: { degraded?: boolean };
    context_watch?: { status?: string } | null;
  };
};

type GuardianWatch = {
  enabled?: boolean;
  next_move?: { copy?: string; presence?: { state?: string } } | null;
};

type DepartureReadiness = {
  state?: string;
  target_title?: string | null;
  target_activity_id?: string | null;
  starts_at?: string | null;
  minutes_until_start?: number | null;
  must_leave_by?: string | null;
  launch_origin?: { source?: string | null; name?: string | null } | null;
  recommended_action?: string;
  departure_activation?: {
    state?: string;
    activity_id?: string | null;
    target_title?: string | null;
    must_leave_by?: string | null;
    minutes_to_leave?: number | null;
    readiness_state?: string;
    can_confirm_departure?: boolean;
    recommended_action?: string;
  } | null;
};

type LaunchSetup = {
  status?: string;
  current_origin?: { name?: string | null } | null;
  candidates?: Array<{ kind?: string; display_name?: string; eligible?: boolean }>;
  actions?: string[];
  contract?: { display_name?: string | null } | null;
};

type RouteOperation = {
  operation_id?: string;
  status?: string;
  outcome?: string | null;
  terminal?: boolean;
  result?: { duration_min?: number | null; mobility_fact_id?: string | null } | null;
};

type Disruption = {
  id: string;
  type: string;
  severity: string;
  /** DETECTED | ACKNOWLEDGED | RESOLVED | IGNORED. Absent means DETECTED. */
  status?: string;
  affected_activity_id: string | null;
  occurred_at: string;
  resolved_at?: string | null;
  payload?: Record<string, unknown>;
};

const EVENT_COPY: Record<string, { zh: string; en: string }> = {
  CLOSURE: { zh: "有一处地点临时关闭", en: "A stop is temporarily closed" },
  WEATHER: { zh: "天气影响行程", en: "Weather is affecting the trip" },
  TRAFFIC_DELAY: { zh: "路上拥堵", en: "Traffic is delayed" },
  SCHEDULE_DELAY: { zh: "行程时间有变化", en: "The schedule has shifted" },
  SERVICE_UNAVAILABLE: { zh: "某项服务不可用", en: "A service is unavailable" },
  USER_CHANGE: { zh: "你更新了行程需求", en: "You changed a trip requirement" },
  OTHER: { zh: "行程有新情况", en: "Something changed" },
};

/**
 * Translate an internal replan refusal into traveller language.
 *
 * `MOBILITY_DATA_UNAVAILABLE` means the engine could not establish how long a
 * new leg takes.  That is not the same finding as "no replacement place
 * exists", so it must not be shown as such (T2 /
 * docs/MOBILITY_TRUTH_CONTRACT.md).  Raw internal codes never reach the
 * traveller; only the Advanced details may retain them.
 */
export function humanReplanReason(raw: string, zh: boolean): string {
  const code = String(raw ?? "");
  if (code.includes("MOBILITY_DATA_UNAVAILABLE")) {
    return zh
      ? "暂时无法获取这段路线的可靠通行时间，因此没有自动修改你的行程。你可以稍后重试，或手动选择替代地点。"
      : "We could not get a reliable travel time for this leg, so your trip was not changed automatically. You can retry later or pick a replacement place yourself.";
  }
  if (code.includes("REPLAN_NO_ALTERNATIVE")) {
    return zh
      ? "附近没有可以替换这处行程的合适地点。"
      : "No suitable replacement place was found for this stop.";
  }
  if (code.includes("REPLAN_TARGET_UNRESOLVED")) {
    return zh
      ? "没能确定这次变化影响的是哪一段行程。"
      : "We could not tell which stop this change affects.";
  }
  if (code.includes("REPLAN_PROTECTED_CONFLICT")) {
    return zh
      ? "这一段属于你已确认不能动的安排，需要你手动处理。"
      : "This stop is part of a protected commitment, so it needs your decision.";
  }
  // Unknown / unmapped reason: never surface the raw internal string.
  return zh
    ? "暂时无法自动调整这段行程，请稍后重试或手动处理。"
    : "We couldn't adjust this part of your trip automatically. Please retry later or handle it manually.";
}

export function LiveRecovery({
  journeyId,
  journeyStatus,
  handledEventIds = [],
  onRefreshLocation,
  onAccepted,
}: {
  journeyId: string;
  journeyStatus?: string | null;
  /** Event ids the shell already knows are resolved by an accepted replan. */
  handledEventIds?: string[];
  /** Captures one user-requested foreground location and returns its server observation id. */
  onRefreshLocation?: () => Promise<string | null>;
  onAccepted?: () => void;
}) {
  const { locale } = useLocale();
  const t = useT();
  const zh = locale.startsWith("zh");
  const twin = useAuthoritativeTwin(journeyId);
  const plans = useAuthoritativePlans(journeyId);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [guardianPulse, setGuardianPulse] = useState<GuardianPulse | null>(null);
  const [guardianWatch, setGuardianWatch] = useState<GuardianWatch | null>(null);
  const [departureReadiness, setDepartureReadiness] = useState<DepartureReadiness | null>(null);
  const [launchSetup, setLaunchSetup] = useState<LaunchSetup | null>(null);
  const [routeOperation, setRouteOperation] = useState<RouteOperation | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [operator, setOperator] = useState<JourneyOperatorProjection | null>(null);
  const [transitReroute, setTransitReroute] = useState<InTransitRerouteResult | null>(null);
  const [groupRoute, setGroupRoute] = useState<GroupRendezvousRouteResult | null>(null);
  const [groupRecoveryResult, setGroupRecoveryResult] = useState<GroupRecoveryPlanResult | null>(null);
  const [intentText, setIntentText] = useState("");
  const [intentResult, setIntentResult] = useState<string | null>(null);
  const [inboxText, setInboxText] = useState("");
  const [inboxResult, setInboxResult] = useState<string | null>(null);
  const groupRecoveryApplyKey = useRef<string | null>(null);

  const loadOperator = useCallback(async () => {
    try {
      setOperator(await cityNexusApi.getJourneyOperator(journeyId));
    } catch {
      setOperator(null);
    }
  }, [journeyId]);

  useEffect(() => {
    void loadOperator();
  }, [loadOperator]);

  const startOperator = useCallback(async () => {
    setBusy("operator-start");
    setMessage(null);
    try {
      const next = await cityNexusApi.startJourneyOperator(journeyId);
      setOperator(next);
      setMessage(zh ? "CityNexus 会照看这趟行程。" : "CityNexus is now taking care of this trip.");
    } catch {
      setMessage(zh ? "暂时无法开启行程托管。" : "We couldn't start trip care yet.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, zh]);

  const submitJourneyIntent = useCallback(async () => {
    const command = intentText.trim();
    if (!command) return;
    setBusy("journey-intent");
    setIntentResult(null);
    try {
      const result = await cityNexusApi.steerJourneyIntent(journeyId, command);
      setIntentResult(result.message);
      setIntentText("");
      await loadOperator();
      onAccepted?.();
    } catch {
      setIntentResult(zh ? "暂时无法调整这趟行程，请稍后再试。" : "I couldn’t steer this Journey right now. Please try again later.");
    } finally {
      setBusy(null);
    }
  }, [intentText, journeyId, loadOperator, onAccepted, zh]);

  const submitJourneyInbox = useCallback(async () => {
    const text = inboxText.trim();
    if (!text) return;
    setBusy("journey-inbox");
    try {
      const result = await cityNexusApi.submitJourneyInboxText(journeyId, text) as { status?: string; impact?: string; replayed?: boolean };
      setInboxResult(result.status === "APPLIED"
        ? (zh ? "已更新预订信息，CityNexus 正在检查后续影响。" : "Reservation updated. CityNexus is checking what comes next.")
        : (zh ? "已收到这条预订信息，还需要核对一个细节。" : "Booking update received. One detail needs checking."));
      setInboxText("");
      await loadOperator();
      onAccepted?.();
    } catch {
      setInboxResult(zh ? "无法处理这条预订信息，请稍后重试。" : "This booking update could not be processed. Please try again.");
    } finally { setBusy(null); }
  }, [inboxText, journeyId, loadOperator, onAccepted, zh]);

  const bindInboxReservation = useCallback(async (itemId: string, reservationId: string) => {
    setBusy("inbox-binding");
    try {
      await cityNexusApi.bindJourneyInboxItem(journeyId, itemId, reservationId);
      await loadOperator();
      onAccepted?.();
    } catch { setMessage(zh ? "无法绑定这项预订，请刷新后重试。" : "That reservation could not be matched. Refresh and try again."); }
    finally { setBusy(null); }
  }, [journeyId, loadOperator, onAccepted, zh]);

  const resolveRealityConflict = useCallback(async (conflictId: string, claimId: string) => {
    setBusy("reality-resolution");
    try {
      await cityNexusApi.resolveJourneyRealityConflict(journeyId, conflictId, claimId);
      await loadOperator();
      setMessage(zh ? "已记录你的选择，CityNexus 正在重新评估后续影响。" : "Choice recorded. CityNexus is checking the Journey impact again.");
      onAccepted?.();
    } catch { setMessage(zh ? "无法记录这个选择，请刷新后重试。" : "That choice could not be recorded. Refresh and try again."); }
    finally { setBusy(null); }
  }, [journeyId, loadOperator, onAccepted, zh]);

  const answerAttentionIntent = useCallback(async (intentId: string, answer: string) => {
    setBusy("intent-clarification");
    setMessage(null);
    try {
      const next = await cityNexusApi.answerJourneyIntentClarification(journeyId, intentId, answer);
      setOperator(next);
      const blocked = next.intent_clarification_result?.outcome === "DECISION_INBOX_BRAIN_BLOCKED_AFTER_RESPONSE";
      setMessage(blocked
        ? (zh ? "已记录你的选择；规划 Agent 暂不可用，原行程保持不变。" : "Your answer is saved. The planning Agent is unavailable; your current trip is unchanged.")
        : (zh ? "已记住你的选择，CityNexus 正在继续处理。" : "Got it. CityNexus is continuing with your Journey."));
      await loadOperator();
      onAccepted?.();
    } catch {
      setMessage(zh ? "无法提交这个选择，请刷新后重试。" : "That choice could not be submitted. Refresh and try again.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, loadOperator, onAccepted, zh]);

  const approveOperatorChange = useCallback(async () => {
    const approvalId = operator?.approval?.approval_id;
    if (!approvalId) return;
    setBusy("operator-approve");
    setMessage(null);
    try {
      await cityNexusApi.approveJourneyOperatorChange(journeyId, approvalId);
      await loadOperator();
      await plans.refetch();
      await twin.refetch();
      setMessage(zh ? "变更已应用，CityNexus 会继续照看行程。" : "Change applied. CityNexus is watching your trip again.");
      onAccepted?.();
    } catch {
      setMessage(zh ? "这项变更暂时无法批准。" : "We couldn't approve that change yet.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, loadOperator, onAccepted, operator, plans, twin, zh]);

  const rejectOperatorChange = useCallback(async () => {
    const approvalId = operator?.approval?.approval_id;
    if (!approvalId) return;
    setBusy("operator-reject");
    setMessage(null);
    try {
      await cityNexusApi.rejectJourneyOperatorChange(journeyId, approvalId);
      await loadOperator();
      setMessage(zh ? "保留当前行程，CityNexus 会继续照看。" : "Keeping the current plan. CityNexus will keep watching.");
    } catch {
      setMessage(zh ? "暂时无法保留当前行程。" : "We couldn't keep the current plan yet.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, loadOperator, operator, zh]);

  const answerCollectiveOption = useCallback(async (proposalId: string | null) => {
    const collective = operator?.collective;
    if (!collective?.mission_id || !collective.clarification_id || !collective.viewer_member_id || !proposalId) return;
    setBusy("collective-answer");
    setMessage(null);
    try {
      await cityNexusApi.answerCollectiveClarification(
        journeyId,
        collective.mission_id,
        collective.viewer_member_id,
        collective.clarification_id,
        proposalId,
      );
      await loadOperator();
      setMessage(zh ? "已记录你的选择，正在等待其他成员。" : "Your choice is recorded. Waiting for the other travellers.");
    } catch {
      setMessage(zh ? "这次选择已过期或暂时无法提交。" : "That choice is stale or could not be submitted yet.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, loadOperator, operator, zh]);

  const loadProposals = useCallback(async () => {
    try {
      const r = await fetch(`/api/v1/journeys/${journeyId}/replans`);
      const body = await r.json();
      setProposals(Array.isArray(body) ? (body as Proposal[]) : []);
    } catch {
      setProposals([]);
    }
  }, [journeyId]);

  useEffect(() => {
    void loadProposals();
  }, [loadProposals]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`/api/v1/journeys/${journeyId}/guardian/pulse/latest`);
        if (!response.ok) return;
        const body = await response.json();
        if (!cancelled) setGuardianPulse((body?.pulse ?? null) as GuardianPulse | null);
      } catch {
        if (!cancelled) setGuardianPulse(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [journeyId]);

  const loadLaunchSetup = useCallback(async () => {
    try {
      const response = await fetch(`/api/v1/journeys/${journeyId}/launch-setup`);
      if (!response.ok) return;
      setLaunchSetup((await response.json()) as LaunchSetup);
    } catch {
      setLaunchSetup(null);
    }
  }, [journeyId]);

  useEffect(() => {
    void loadLaunchSetup();
  }, [loadLaunchSetup]);

  const confirmLaunchSource = useCallback(async (sourceKind: "SELECTED_STAY" | "ARRIVAL_ANCHOR") => {
    setBusy(`launch:${sourceKind}`);
    try {
      const response = await fetch(`/api/v1/journeys/${journeyId}/launch-origin`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_kind: sourceKind }),
      });
      if (!response.ok) throw new Error("launch origin unavailable");
      await loadLaunchSetup();
      const readiness = await fetch(`/api/v1/journeys/${journeyId}/guardian/readiness`);
      if (readiness.ok) setDepartureReadiness((await readiness.json()) as DepartureReadiness);
    } catch {
      setMessage(zh ? "暂时无法保存出发点。" : "We couldn't save that starting point yet.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, loadLaunchSetup, zh]);

  const checkRoute = useCallback(async () => {
    setBusy("route");
    try {
      const response = await fetch(`/api/v1/journeys/${journeyId}/routes/acquire`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requested_mode: "WALK" }),
      });
      if (!response.ok) throw new Error("route unavailable");
      const body = (await response.json()) as RouteOperation;
      setRouteOperation(body);
      const readiness = await fetch(`/api/v1/journeys/${journeyId}/guardian/readiness`);
      if (readiness.ok) setDepartureReadiness((await readiness.json()) as DepartureReadiness);
    } catch {
      setMessage(zh ? "暂时无法检查这段路线。" : "We couldn't check that route yet.");
    } finally {
      setBusy(null);
    }
  }, [journeyId, zh]);

  const confirmDeparture = useCallback(async () => {
    const activation = departureReadiness?.departure_activation;
    const activityId = activation?.activity_id ?? departureReadiness?.target_activity_id;
    if (!activityId || activation?.can_confirm_departure !== true || activation.state === "BLOCKED") {
      setMessage(zh ? "现在还不能确认出发，请先检查行程提示。" : "You cannot confirm departure yet. Check the trip guidance first.");
      return;
    }
    setBusy("departure");
    setMessage(null);
    try {
      const response = await fetchWithWriteDeadline(
        `/api/v1/journeys/${journeyId}/activities/${activityId}/depart`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", ...travelerAuthHeaders() },
          body: JSON.stringify({
            truth_kind: "OPERATOR_CONFIRMED_EXECUTION",
            operator_confirmed: true,
          }),
        },
      );
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          setMessage(zh ? "请先登录后确认出发。" : "Please sign in before confirming departure.");
        } else {
          setMessage(zh ? "暂时无法确认出发。" : "We couldn't confirm departure yet.");
        }
        return;
      }
      setMessage(zh ? "已记录，你正在前往下一站。" : "Got it — you’re on the way to the next stop.");
      const readiness = await fetch(`/api/v1/journeys/${journeyId}/guardian/readiness`);
      if (readiness.ok) setDepartureReadiness((await readiness.json()) as DepartureReadiness);
      await twin.refetch();
      await plans.refetch();
    } catch (error) {
      setMessage(isWriteDeadlineMissed(error) ? writeDeadlineCopy(zh).body : (zh ? "暂时无法确认出发。" : "We couldn't confirm departure yet."));
    } finally {
      setBusy(null);
    }
  }, [departureReadiness, journeyId, plans, twin, zh]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await fetch(`/api/v1/journeys/${journeyId}/guardian/autopilot/status`);
        if (!response.ok) return;
        const body = await response.json();
        if (!cancelled) {
          setGuardianWatch(
            body?.enrollment?.enabled
              ? ({ enabled: true, next_move: body?.watch?.next_move ?? null } as GuardianWatch)
              : null,
          );
          setDepartureReadiness(body?.readiness ?? null);
        }
      } catch {
        if (!cancelled) setGuardianWatch(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [journeyId]);

  const plan =
    plans.data?.find((p) => p.status === "ACTIVE" || p.status === "SELECTED") ?? null;
  const journeyCompleted =
    String(journeyStatus ?? twin.data?.context?.journey_status ?? "").toUpperCase() ===
    "COMPLETED";
  const activities = (plan?.activities ?? []) as unknown as Array<Record<string, unknown>>;
  // The projection this card reads is the shared execution truth, whose keys are
  // now_activity_id / next_activity_id / cursor_status / persisted_cursor.  It used to
  // reach for `current_activity_id`, a key that dict does not contain (the *twin* has
  // one at its top level; `execution` does not), so `currentId` was permanently null and
  // the `?? activities[0]` below made the first stop the traveller's Now no matter how
  // far the journey had advanced — CX-SOL-001's failure class in a second consumer.
  const context = twin.data?.context as
    | {
        disruptions?: Disruption[];
        execution?: {
          now_activity_id?: string | null;
          cursor_status?: string | null;
          persisted_cursor?: string | null;
        };
      }
    | undefined;
  // Also read the plan-scoped disruption list from /experience: the twin
  // context can be empty immediately after a replan while the Journey still
  // needs to explain what happened.
  const [disruptions, setDisruptions] = useState<Disruption[]>([]);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch(`/api/v1/journeys/${journeyId}/experience`);
        const body = await r.json();
        const fromPlan = (body?.active_plan?.disruptions ?? []) as Disruption[];
        const fromContext = (context?.disruptions ?? []) as Disruption[];
        if (cancelled) return;
        const merged = new Map<string, Disruption>();
        for (const item of [...fromPlan, ...fromContext]) merged.set(item.id, item);
        setDisruptions([...merged.values()]);
      } catch {
        if (!cancelled) setDisruptions((context?.disruptions ?? []) as Disruption[]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [journeyId, context]);
  // An event the traveller already acted on must not keep shouting "what
  // happened" as an open problem.  Acknowledgement is deliberately NOT
  // resolution in this product: `ACKNOWLEDGED` means the traveller has seen
  // the disruption, not that a recovery was chosen.  Only RESOLVED, IGNORED,
  // or an ACCEPTED replan explicitly linked to this event closes it
  // (T3 / docs/ACK_RESOLUTION_CONTRACT.md).  The rule lives in one shared
  // module so Today and Live cannot disagree about the same event.
  const acceptedTriggerIds: Set<string> = new Set(
    proposals
      .filter((p) => p.status === "ACCEPTED")
      .map((p) => String((p as { trigger_event_id?: string | null }).trigger_event_id ?? ""))
      .filter(Boolean),
  );
  const leaveByDisplay = departureReadiness?.must_leave_by
    ? new Date(departureReadiness.must_leave_by).toLocaleTimeString(locale, {
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;
  const openDisruptions = disruptions.filter((d) => isDisruptionOpen(d, acceptedTriggerIds));
  const handledDisruptions = disruptions.filter((d) => !isDisruptionOpen(d, acceptedTriggerIds));
  // Acknowledged-but-unresolved events get their own copy: the traveller has
  // seen them, but they still need a decision.
  const acknowledgedOnly = disruptions.filter((d) =>
    isDisruptionAcknowledgedOnly(d, acceptedTriggerIds),
  );

  const contextStatus = String(guardianPulse?.result?.context_watch?.status ?? "");
  const pulseDegraded = Boolean(guardianPulse?.result?.signals?.degraded);
  const guardianSurfaceState = guardianPulse
    ? contextStatus === "MATERIAL_CONFLICT" || openDisruptions.length > 0
      ? "recovery"
      : pulseDegraded || ["UNKNOWN", "PROVIDER_DEGRADED"].includes(contextStatus) || guardianPulse.status !== "COMPLETED"
        ? "incomplete"
        : "clear"
    : null;

  // Canonicalise each proposal's semantic diff so two proposals that describe
  // the SAME change cannot appear as two identical "接受新方案" buttons.  The key
  // is order-insensitive and normalised, so id ordering or case cannot create a
  // spurious "alternative".
  const diffKey = (p: Proposal): string => {
    const norm = (values?: string[]) =>
      [...new Set((values ?? []).map((v) => String(v).trim().toLowerCase()))].sort();
    return JSON.stringify({
      removed: norm(p.diff?.removed),
      added: norm(p.diff?.added),
      moved: norm(p.diff?.moved),
    });
  };
  // A completed Journey has no executable cursor.  Older databases can still
  // contain PRESENTED rows from before the server retired them at completion;
  // keep those rows available to history APIs, but never render them as an
  // actionable or advisory recovery card on the completed surface.
  const pendingAll = journeyCompleted
    ? []
    : proposals.filter((p) => p.status === "PRESENTED" || p.status === "GENERATED");
  const seenDiff = new Set<string>();
  const pending = pendingAll.filter((p) => {
    const key = diffKey(p);
    if (seenDiff.has(key)) return false;
    seenDiff.add(key);
    return true;
  });
  const duplicateCount = pendingAll.length - pending.length;
  const cursorStatus = context?.execution?.cursor_status ?? null;
  const nowId = context?.execution?.now_activity_id ?? null;
  // No list position is ever a substitute for a resolved Now: an id that the plan
  // cannot honour yields null, and the render below says so instead of pointing at
  // whatever happens to be first.
  const liveNow = nowId ? (activities.find((a) => a.id === nowId) ?? null) : null;
  // Recent events that were already handled still deserve an explanation.
  const handled = proposals.filter((p) => p.status === "ACCEPTED").slice(0, 1);

  const accept = async (proposalId: string) => {
    setBusy(proposalId);
    setMessage(null);
    try {
      const r = await fetchWithWriteDeadline(`/api/v1/replans/${proposalId}/accept`, {
        method: "POST",
        headers: { "Idempotency-Key": `accept-${proposalId}` },
      });
      if (!r.ok) {
        const body = await r.json().catch(() => null);
        const raw =
          body?.error?.message ??
          body?.detail ??
          t("live.acceptFailed");
        setMessage(humanReplanReason(raw, zh));
        return;
      }
      setMessage(zh ? "已按新安排更新你的行程。" : "Your trip has been updated.");
      await loadProposals();
      await plans.refetch();
      await twin.refetch();
      onAccepted?.();
    } catch (error) {
      /* ORD-0281 T-b: this device's bound expiring is its own fact.  It used to be reported as
         「连不上服务器」, which blamed the network for a request the server may still be finishing --
         the same attribution mistake ORD-0280 closed for the label, in another panel. */
      if (isWriteDeadlineMissed(error)) {
        const copy = writeDeadlineCopy(zh);
        setMessage(`${copy.title}｜${copy.body}`);
      } else {
        setMessage(zh ? "连不上服务器，请重试。" : "Could not reach the server. Try again.");
      }
    } finally {
      setBusy(null);
    }
  };

  const defer = () =>
    setMessage(zh ? "好的，稍后再提醒你。" : "Okay, we will remind you later.");

  const flow = operator?.flow ?? null;
  const transit = operator?.transit ?? null;
  const groupPresence = operator?.group_presence ?? null;
  const viewerMember = groupPresence?.members.find((member) => member.is_viewer) ?? null;
  const momentScoutResetKey = [
    journeyId,
    viewerMember?.member_id ?? "",
    groupPresence?.active_plan_version_id ?? "",
    groupPresence?.current_activity_id ?? "",
    operator?.flow?.current?.id ?? "",
    operator?.flow?.next?.id ?? "",
    operator?.ahead?.current_activity_id ?? "",
    operator?.member_side_quest?.id ?? "",
    operator?.member_side_quest?.state ?? "",
  ].join("|");
  const plannedSeparation = groupPresence?.planned_separation ?? null;
  const groupRecovery = operator?.group_recovery ?? null;
  const recoveryFollowthrough = groupRecovery?.followthrough ?? null;
  const collectiveProposalIds = new Set(
    groupRecovery?.collective_decision?.proposal_ids ?? [],
  );
  const individuallyActionablePending = pending.filter(
    (proposal) => !collectiveProposalIds.has(proposal.id),
  );
  const showGroupRecovery = Boolean(groupRecovery && groupRecovery.foreground !== false && [
    "ADVISORY_MEMBER_LATE",
    "VERIFIED_MEMBER_RISK",
    "RECOVERY_OPTIONS_READY",
    "COLLECTIVE_DECISION_REQUIRED",
    "RECOVERY_PROPOSAL_READY",
    "RECOVERY_EXTERNAL_AGENT_BLOCKED",
    "RECOVERY_REPLAN_REQUIRED",
    "RECOVERY_REGROUNDING",
    "RECOVERY_REGROUNDED_NO_ACTION",
    "RECOVERY_REGROUNDED_DOMINANT",
    "RECOVERY_REGROUNDED_DECISION_REQUIRED",
    "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED",
    "RECOVERY_REGROUND_NO_FEASIBLE_OPTION",
    "RECOVERY_APPLIED",
    "RECOVERY_APPLIED_REJOIN_PENDING",
    "REJOIN_IN_PROGRESS",
    "REJOIN_CONFIRMATION_REQUIRED",
    "RECOVERY_CLOSED",
    "COLLECTIVE_WAITING_FOR_MEMBERS",
    "COLLECTIVE_MEDIATING",
    "COLLECTIVE_AUTHORIZED",
    "COLLECTIVE_STALE",
    "COLLECTIVE_DECISION_REQUIRED",
  ].includes(groupRecovery.state));
  const applySharedRecovery = async () => {
    const decision = groupRecovery?.collective_decision;
    if (busy !== null || !decision?.viewer_can_apply) return;
    if (!groupRecoveryApplyKey.current) {
      const nonce = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      groupRecoveryApplyKey.current = `group-recovery-${nonce}`;
    }
    const idempotencyKey = groupRecoveryApplyKey.current;
    setBusy("group-recovery-apply");
    setMessage(null);
    try {
      const result = await cityNexusApi.applyGroupRecovery(journeyId, idempotencyKey);
      if (result.group_recovery_apply?.result !== "APPLIED") {
        throw new Error("GROUP_RECOVERY_APPLY_NOT_CONFIRMED");
      }
      groupRecoveryApplyKey.current = null;
      await Promise.all([loadOperator(), loadProposals(), plans.refetch(), twin.refetch()]);
      setMessage(zh
        ? "共同同意的调整已应用；新的行程安排已经生效。"
        : "The group-approved recovery is applied and the updated Journey is active.");
      onAccepted?.();
    } catch (error) {
      if (error instanceof CityNexusApiError && error.status >= 400 && error.status < 500) {
        groupRecoveryApplyKey.current = null;
        if (error.status === 409) {
          const body = error.body as {
            error?: {
              reason_code?: unknown;
              details?: unknown;
            };
          } | null;
          const details = body?.error?.details;
          const code = details && typeof details === "object" && "code" in details
            ? String((details as { code?: unknown }).code ?? "")
            : String(body?.error?.reason_code ?? "");
          await Promise.all([loadOperator(), loadProposals()]);
          setMessage(code === "GROUP_RECOVERY_REPLAN_REQUIRED"
            ? (zh
              ? "共同选择仍作为历史保留，但这个具体方案已不再安全可执行；CityNexus 需要先重新规划，而不是让大家对同一个失效方案再投一次。"
              : "The group’s earlier choice is preserved, but this concrete plan is no longer safe to execute. CityNexus needs a fresh grounded recovery rather than asking everyone to vote on the same invalid plan again.")
            : (zh
              ? "原来的共同选择已过期或现实已变化；我刷新了当前状态，没有重复应用旧方案。"
              : "The shared choice is stale or the trip changed. Current options were refreshed; the old plan was not applied."));
        } else if (error.status === 401 || error.status === 403) {
          setMessage(zh
            ? "当前登录成员没有应用这项共享行程变更的权限。"
            : "The signed-in traveller is not authorized to apply this shared Journey change.");
        } else {
          setMessage(zh ? "暂时无法应用这项共享调整。" : "The shared recovery could not be applied.");
        }
      } else {
        // Keep the key across an ambiguous transport failure so a retry reads
        // the durable result instead of spending the same consent twice.
        setMessage(zh
          ? "暂时没有收到服务器确认；重试会查询同一提交结果，不会再花一次同意。"
          : "No server confirmation arrived. Retrying will reuse the same idempotency key.");
      }
    } finally {
      setBusy(null);
    }
  };
  const confirmGroupRejoin = async () => {
    if (busy !== null || !recoveryFollowthrough?.viewer_can_confirm) return;
    setBusy("group-recovery-rejoin");
    setMessage(null);
    try {
      const rendezvousActivityId = recoveryFollowthrough.rendezvous?.activity_id;
      if (!rendezvousActivityId) return;
      await cityNexusApi.transitionActivity(journeyId, rendezvousActivityId, "arrive");
      const refreshed = await cityNexusApi.getJourneyOperator(journeyId);
      setOperator(refreshed);
      await twin.refetch();
      const closed = refreshed.group_recovery?.followthrough?.state === "RECOVERY_CLOSED";
      setMessage(closed
        ? (zh
          ? "所有仍在旅程中的同行者都已确认会合；恢复流程已结束，CityNexus 继续照看当前行程。"
          : "Everyone still on the Journey has confirmed rejoining. Recovery is closed and CityNexus is continuing the current trip.")
        : (zh
          ? "已通过行程到达状态记录你的本人确认；正在等待其他同行者。"
          : "Your arrival was recorded through the Journey execution flow. Waiting for the remaining travellers."));
      onAccepted?.();
    } catch (error) {
      if (error instanceof CityNexusApiError && (error.status === 401 || error.status === 403)) {
        setMessage(zh ? "请用自己的有效 Journey 成员账号确认；不能替其他同行者确认。" : "Sign in as yourself to confirm; you cannot report for another traveller.");
      } else if (error instanceof CityNexusApiError && error.status === 409) {
        await loadOperator();
        setMessage(zh ? "行程或成员状态已经变化，我刷新了当前会合状态；旧确认没有被套用。" : "The Journey or membership changed. Current rejoin state was refreshed; no stale confirmation was applied.");
      } else {
        setMessage(zh ? "暂时无法记录会合确认，请刷新后重试。" : "Could not save the rejoin confirmation. Refresh and try again.");
      }
    } finally {
      setBusy(null);
    }
  };
  const requestFreshGroupRoute = async (): Promise<GroupRendezvousRouteResult | null> => {
    if (!onRefreshLocation || !groupPresence?.can_guide) return null;
    const observationId = await onRefreshLocation();
    if (!observationId) return null;
    const result = await cityNexusApi.routeToGroupRendezvous(journeyId, observationId);
    setGroupRoute(result);
    await loadOperator();
    return result;
  };
  const guideToGroup = async () => {
    if (!onRefreshLocation || busy !== null || !groupPresence?.can_guide) {
      setMessage(zh ? "请先允许获取你这一次的位置。" : "Allow one fresh location sample to get a route.");
      return;
    }
    setBusy("group-route");
    setGroupRoute(null);
    try {
      const result = await requestFreshGroupRoute();
      if (!result) {
        setMessage(zh ? "没有取得这一次的位置；共同会合点保持不变。" : "No one-time location was available; the shared rendezvous is unchanged.");
        return;
      }
      setMessage(
        result.status === "COMPLETED" && result.route.duration_min != null
          ? (zh ? `到「${result.target.title}」约 ${result.route.duration_min} 分钟；位置只用于这次路线，行程计划未变。` : `About ${result.route.duration_min} min to ${result.target.title}. This one-time location was used for routing; your plan is unchanged.`)
          : (zh ? "路线服务暂时没有可靠结果；没有修改行程计划。" : "No reliable route is available yet. Your plan was not changed."),
      );
    } catch {
      setMessage(zh ? "暂时无法计算到集合点的路线；计划没有改变。" : "Could not calculate a route to the rendezvous. Your plan is unchanged.");
    } finally {
      setBusy(null);
    }
  };
  const routeAfterPrivateStop = async (): Promise<GroupRendezvousRouteResult | null> => {
    if (busy !== null || !onRefreshLocation || !groupPresence?.can_guide) return null;
    setBusy("side-quest-head-back-route");
    setGroupRoute(null);
    try {
      return await requestFreshGroupRoute();
    } catch {
      return null;
    } finally {
      setBusy(null);
    }
  };
  const requestGroupRecovery = async () => {
    if (busy !== null || !groupRecovery?.can_request_planning) return;
    setBusy("group-recovery");
    setGroupRecoveryResult(null);
    try {
      const result = await cityNexusApi.requestGroupRecoveryPlan(journeyId);
      setGroupRecoveryResult(result);
      await loadOperator();
      setMessage(
        result.status === "RECOVERY_EXTERNAL_AGENT_BLOCKED"
          ? (zh ? "Agent 暂时不可用；当前全组行程没有改变。" : "The Agent is unavailable; the shared plan is unchanged.")
          : (zh ? "已交给 CityNexus 准备一个受治理的全组方案；它不会自行改行程。" : "CityNexus is preparing a governed group option; it will not change the trip by itself."),
      );
    } catch {
      setMessage(zh ? "暂时无法启动分组恢复；当前行程没有改变。" : "Could not start group recovery; the shared plan is unchanged.");
    } finally {
      setBusy(null);
    }
  };
  const rerouteFromHere = async () => {
    if (!onRefreshLocation || busy !== null) {
      setMessage(zh ? "请先允许浏览器获取这次位置。" : "Allow location access to recalculate this route.");
      return;
    }
    setBusy("transit-reroute");
    setTransitReroute(null);
    try {
      const observationId = await onRefreshLocation();
      if (!observationId) return;
      const result = await cityNexusApi.rerouteInTransit(journeyId, observationId);
      setTransitReroute(result);
      await loadOperator();
      onAccepted?.();
      setMessage(
        result.status === "COMPLETED" && result.reroute_state !== "IN_TRANSIT_CONTEXT_STALE"
          ? (zh ? "已从刚才的位置重新计算路线；原计划没有改变。" : "A route was recalculated from your current location. Your plan is unchanged.")
          : result.reroute_state === "IN_TRANSIT_CONTEXT_STALE"
            ? (zh ? "路线计算期间行程状态已变化；这条路线没有作为当前行程建议。" : "The trip changed while the route was being calculated, so this route was not presented as current advice.")
            : (zh ? "暂时无法从当前位置取得可靠路线，行程计划未变。" : "A reliable route is not available from here yet. Your plan is unchanged."),
      );
    } catch {
      setMessage(
        zh
          ? "暂时无法重新计算路线；你仍可按原计划前往，行程没有改变。"
          : "We couldn't recalculate the route yet. Your plan is unchanged.",
      );
    } finally {
      setBusy(null);
    }
  };
  const advanceFlow = async (transition: "depart" | "arrive" | "begin" | "complete") => {
    const activityId = flow?.current?.id;
    if (!activityId || !flow?.can_confirm || busy !== null) return;
    setBusy(`flow:${transition}`);
    setMessage(null);
    try {
      await cityNexusApi.transitionActivity(journeyId, activityId, transition);
      await Promise.all([loadOperator(), twin.refetch(), plans.refetch()]);
      setMessage(zh ? "已更新行程进度。" : "Trip progress updated.");
    } catch {
      setMessage(zh ? "请先登录后确认这一步。" : "Please sign in before confirming this step.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="v8-page" data-testid="live-recovery">
      <div className="v8-page-intro compact">
        <div>
          <p className="v8-kicker">
            {journeyCompleted
              ? zh
                ? "已完成"
                : "Completed"
              : zh
                ? "正在进行"
                : "In progress"}
          </p>
          <h1>
            {journeyCompleted
              ? t("live.completedTitle")
              : zh
                ? "现在的行程"
                : "Your trip right now"}
          </h1>
          <p
            className="v8-lede"
            data-testid="live-next"
            data-cursor-status={cursorStatus ?? "UNKNOWN"}
            data-now-activity-id={String(liveNow?.id ?? "")}
          >
            {journeyCompleted
              ? t("live.completedAdvisory")
              : liveNow
                  ? zh
                    ? `下一站：${String(liveNow.title ?? "")}`
                    : `Next: ${String(liveNow.title ?? "")}`
                  : !plan
                    ? t("live.awaitingPlan")
                    : cursorStatus === "STALE"
                      ? t("live.unresolvedStep")
                      : t("live.nothingDue")}
          </p>
        </div>
      </div>

      <section className="journey-operator-card" data-testid="journey-operator" aria-live="polite">
        <div className="journey-operator-heading">
          <p className="v8-kicker">Journey Operator</p>
          <strong>{operator?.headline ?? (zh ? "把这趟行程交给 CityNexus" : "Let CityNexus take care of this trip")}</strong>
        </div>
        {operator?.attention && (
          <section className="journey-operator-attention" data-testid="journey-operator-attention" aria-label={zh ? "需要你处理" : "Needs you"}>
            <span className="v8-kicker">{zh ? "需要你" : "Needs you"}</span>
            <strong>{operator.attention.title}</strong>
            <span>{operator.attention.body}</span>
            {operator.attention.action_type === "ANSWER_INTENT_CLARIFICATION" && (
              <div className="journey-operator-attention-actions">
                {(operator.attention.options ?? []).map((option) => (
                  <button key={option.value} type="button" disabled={busy !== null}
                    onClick={() => void answerAttentionIntent(operator.attention!.source_ref.intent_id, option.value)}>
                    {option.label}
                  </button>
                ))}
              </div>
            )}
            {operator.attention.action_type === "BIND_INBOX_ITEM" && (
              <div className="journey-operator-attention-actions">
                {(operator.attention.options ?? []).map((option) => <button key={option.value} type="button" disabled={busy !== null}
                  onClick={() => void bindInboxReservation(operator.attention!.source_ref.inbox_item_id, option.value)}>{option.label}</button>)}
              </div>
            )}
            {operator.attention.action_type === "RESOLVE_REALITY_CONFLICT" && (
              <div className="journey-operator-attention-actions" data-testid="reality-conflict-choices">
                {(operator.attention.options ?? []).map((option) => <button key={option.value} type="button" disabled={busy !== null}
                  onClick={() => void resolveRealityConflict(operator.attention!.source_ref.conflict_id, option.value)}>{option.label}</button>)}
              </div>
            )}
            {operator.attention.action_type === "REVIEW_APPROVAL" && operator.approval?.required && (
              <button type="button" disabled={busy !== null} onClick={() => void approveOperatorChange()}>{operator.attention.action_label}</button>
            )}
            {operator.attention.action_type === "RESPOND_COLLECTIVE" && operator.collective?.viewer_can_respond && (
              <div className="journey-operator-attention-actions">
                {operator.collective.options.map((option) => <button key={option.proposal_id ?? option.label} type="button" disabled={busy !== null} onClick={() => void answerCollectiveOption(option.proposal_id)}>{option.label}</button>)}
              </div>
            )}
            {operator.attention.action_type === "CHECK_ROUTE" && <button type="button" disabled={busy !== null} onClick={() => void checkRoute()}>{operator.attention.action_label}</button>}
            {operator.attention.action_type?.startsWith("CONFIRM_") && (
              <button type="button" disabled={busy !== null} onClick={() => void advanceFlow(({ CONFIRM_ARRIVAL: "arrive", CONFIRM_BEGIN: "begin", CONFIRM_COMPLETE: "complete", CONFIRM_DEPARTURE: "depart" } as const)[operator.attention!.action_type as "CONFIRM_ARRIVAL" | "CONFIRM_BEGIN" | "CONFIRM_COMPLETE" | "CONFIRM_DEPARTURE"])}>{operator.attention.action_label}</button>
            )}
            {operator.attention.action_type === "REVIEW_TRANSACTION" && <a href="#journey-operator-operations">{operator.attention.action_label}</a>}
          </section>
        )}
        {flow && (
          <div className={`journey-continuous-flow journey-flow-${flow.state.toLowerCase()}`} data-testid="journey-continuous-flow">
            <span className="v8-kicker">{zh ? "今天的行程" : "Today’s journey"}</span>
            <strong data-testid="journey-flow-state">
              {flow.state === "DAY_COMPLETE" ? (zh ? "今天的行程完成了。" : "Your journey is complete.")
                : flow.state === "NEEDS_ATTENTION" ? (zh ? "下一步需要你的注意。" : "The next step needs your attention.")
                : flow.current ? (zh ? `现在：${flow.current.title}` : `Now: ${flow.current.title}`)
                : (zh ? "正在准备下一步。" : "Preparing the next step.")}
            </strong>
            {flow.next && flow.state !== "DAY_COMPLETE" && (
              <span>{zh ? `接下来：${flow.next.title}` : `Next: ${flow.next.title}`}</span>
            )}
            {flow.route?.exact && flow.route.duration_min != null ? (
              <small>{zh ? `已找到准确路线 · 约 ${flow.route.duration_min} 分钟` : `Exact route found · about ${flow.route.duration_min} min`}</small>
            ) : flow.current && ["PREPARING_TO_LEAVE", "NEXT_STOP_PREPARING", "BETWEEN_STOPS"].includes(flow.state) && operator?.attention?.action_type !== "CHECK_ROUTE" ? (
              <>
                <small>{zh ? "路线还需要检查。" : "The route still needs checking."}</small>
                <button type="button" disabled={busy !== null} onClick={() => void checkRoute()}>{zh ? "检查路线" : "Check route"}</button>
              </>
            ) : null}
            {flow.state === "DAY_COMPLETE" ? null : flow.state === "ARRIVAL_READY" && operator?.attention?.action_type !== "CONFIRM_ARRIVAL" ? (
              <button type="button" disabled={busy !== null} onClick={() => void advanceFlow("arrive")}>{zh ? "我到了" : "I’m here"}</button>
            ) : flow.state === "READY_TO_BEGIN" && operator?.attention?.action_type !== "CONFIRM_BEGIN" ? (
              <button type="button" disabled={busy !== null} onClick={() => void advanceFlow("begin")}>{zh ? "开始" : "Start"}</button>
            ) : (flow.state === "READY_TO_COMPLETE" || flow.state === "IN_ACTIVITY") && operator?.attention?.action_type !== "CONFIRM_COMPLETE" ? (
              <button type="button" disabled={busy !== null} onClick={() => void advanceFlow("complete")}>{zh ? "完成这站" : "Done"}</button>
            ) : (flow.state === "LEAVE_NOW" || flow.state === "PREPARING_TO_LEAVE") && operator?.attention?.action_type !== "CONFIRM_DEPARTURE" ? (
              <button type="button" disabled={busy !== null} onClick={() => void advanceFlow("depart")}>{zh ? "我出发了" : "I’m leaving"}</button>
            ) : null}
          </div>
        )}
        {journeyStatus === "ACTIVE" && transit?.activity_status === "EN_ROUTE" && (
          <section className="journey-transit-reroute" data-testid="journey-transit-reroute" aria-live="polite">
            <span className="v8-kicker">{zh ? "途中路线" : "On the way"}</span>
            <strong>
              {transit.state === "ADVISORY_DEVIATION"
                ? (zh ? "浏览器位置看起来可能偏离原路线。" : "Your browser location may be off the planned route.")
                : transit.state === "ARRIVAL_READY"
                  ? (zh ? "你似乎已接近目的地；可以使用到达确认。" : "You may be near the stop. You can confirm arrival when ready.")
                  : (zh ? `正在前往：${transit.activity_title ?? "下一站"}` : `Heading to ${transit.activity_title ?? "your next stop"}`)}
            </strong>
            {transit.state === "ROUTE_SHAPE_UNAVAILABLE" || !transit.route_shape_available ? (
              <small>{zh ? "目前没有可比对的原路线形状；不会据此判定偏航，但仍可从你现在的位置重新算路。" : "The original route shape is unavailable, so CityNexus won't claim you are off route. You can still recalculate from your current location."}</small>
            ) : transit.state === "ADVISORY_DEVIATION" ? (
              <small>{zh ? "这只是浏览器位置提示，不会触发 Guardian 或修改行程。" : "This browser location is advisory only. It does not trigger Guardian or change your trip."}</small>
            ) : transit.location_fresh === false ? (
              <small>{zh ? "位置数据已过时；重新计算时会先获取新位置。" : "The saved location is stale. Recalculation will request a fresh location first."}</small>
            ) : null}
            {transit.state !== "ARRIVAL_READY" && (
              <button
                type="button"
                disabled={busy !== null}
                data-testid="journey-transit-reroute-button"
                onClick={() => void rerouteFromHere()}
              >
                {busy === "transit-reroute"
                  ? (zh ? "正在重新计算…" : "Recalculating…")
                  : (zh ? "从我现在的位置重新算路" : "Recalculate from here")}
              </button>
            )}
            {transitReroute?.status === "COMPLETED" && transitReroute.reroute_state !== "IN_TRANSIT_CONTEXT_STALE" && transitReroute.result?.duration_min != null && (
              <small data-testid="journey-transit-reroute-result">
                {zh
                  ? `新路线约 ${transitReroute.result.duration_min} 分钟 · ${transitReroute.provenance_statement ?? transitReroute.result.provenance ?? "来源已记录"} · 原计划未变`
                  : `New route: about ${transitReroute.result.duration_min} min · ${transitReroute.provenance_statement ?? transitReroute.result.provenance ?? "provenance recorded"} · plan unchanged`}
              </small>
            )}
            {transitReroute?.commitment_risk && transitReroute.reroute_state !== "IN_TRANSIT_CONTEXT_STALE" && (
              <small data-testid="journey-transit-commitment-risk">
                {transitReroute.commitment_risk.state === "COMMITMENT_AT_RISK"
                  ? (zh ? `按当前位置路线估算，后续已确认安排「${transitReroute.commitment_risk.next_commitment?.title ?? "下一项承诺"}」可能赶不上；这是${transitReroute.commitment_risk.authority === "ADVISORY_ROUTE" ? "参考位置" : "当前定位"}判断，后续路段沿用已保存的计划时间。` : `The route estimate may put “${transitReroute.commitment_risk.next_commitment?.title ?? "the next protected stop"}” at risk. This uses ${transitReroute.commitment_risk.authority === "ADVISORY_ROUTE" ? "advisory location" : "trusted location"} and frozen planned times for later legs.`)
                  : transitReroute.commitment_risk.state === "WITHIN_BUFFER" || transitReroute.commitment_risk.state === "LOW_BUFFER"
                    ? (zh ? `后续已确认安排「${transitReroute.commitment_risk.next_commitment?.title ?? "下一项承诺"}」预计还剩 ${transitReroute.commitment_risk.slack_after_buffer_min ?? "未知"} 分钟缓冲；后续路段按已保存计划估算，不代表实时路况。` : `Estimated buffer before “${transitReroute.commitment_risk.next_commitment?.title ?? "the next protected stop"}”: ${transitReroute.commitment_risk.slack_after_buffer_min ?? "unknown"} min. Later legs use the saved plan, not live traffic.`)
                    : null}
              </small>
            )}
            {transitReroute && (transitReroute.status !== "COMPLETED" || transitReroute.reroute_state === "IN_TRANSIT_CONTEXT_STALE") && (
              <small data-testid="journey-transit-reroute-result">
                {zh ? "路线服务暂时没有可靠结果；没有修改原计划。" : "The route provider returned no reliable route. Your original plan was not changed."}
              </small>
            )}
          </section>
        )}
        {journeyStatus === "ACTIVE" && groupPresence && groupPresence.state !== "MEMBER_IDENTITY_UNAVAILABLE" && (
          <section
            className="journey-group-presence"
            data-testid="journey-group-presence"
            data-group-state={groupPresence.state}
            data-group-authority={groupPresence.authority}
            aria-live="polite"
          >
            <span className="v8-kicker">{zh ? "同行者位置" : "Group presence"}</span>
            <MemberMeetupHandshakePanel
              journeyId={journeyId}
              projection={groupPresence.meetup_handshake ?? null}
              sideQuest={operator?.member_side_quest ?? null}
              onUpdated={loadOperator}
              showOwner={false}
            />
            <MemberPlaceInvitationPanel
              journeyId={journeyId}
              projection={operator?.member_place_invitations}
              onRefreshLocation={onRefreshLocation}
              onUpdated={loadOperator}
            />
            <strong>
              {groupPresence.state === "PLANNED_SPLIT_ON_TRACK"
                ? (zh ? "有人按计划暂时分开行动；当前路线仍能赶上会合点。" : "A traveller is on a planned side trip and the current route still reaches the rendezvous in time.")
                : groupPresence.state === "PLANNED_SPLIT_RETURN_RISK"
                  ? (zh ? "计划中的分开行动已接近或错过共同会合的安全缓冲。" : "The planned side trip may now threaten the shared rendezvous buffer.")
                  : groupPresence.state === "PLANNED_SPLIT_RETURN_UNKNOWN"
                    ? (zh ? "分开行动是有计划的，但目前没有可靠的返程时间。" : "The split is planned, but CityNexus does not have a reliable return ETA yet.")
                    : groupPresence.state === "SINGLE_TRAVELLER"
                ? (zh ? "这趟行程目前只有你一位有效成员。" : "You are the only active member on this Journey.")
                : groupPresence.state === "GROUP_SPLIT_ADVISORY"
                  ? (zh ? "同行者的位置显示队伍可能已经分开。" : "Recent member locations suggest the group may be split.")
                  : groupPresence.state === "TOGETHER"
                    ? (zh ? "最近分享的位置显示大家仍在一起。" : "Recent shared locations suggest the group is together.")
                    : groupPresence.state === "GROUP_LOCATION_STALE"
                      ? (zh ? "有同行者的位置已经过时或精度不足。" : "Some member locations are stale or too imprecise.")
                      : groupPresence.state === "MEMBER_LOCATION_MISSING"
                        ? (zh ? "正在等待同行者主动分享一次当前位置。" : "Waiting for travellers to share a fresh location.")
                        : (zh ? "目前还不能可靠判断队伍是否在一起。" : "There is not enough current evidence to classify the group yet.")}
            </strong>
            {groupPresence.members.some((member) => member.is_viewer)
              && !["EN_ROUTE", "ARRIVED", "COMPLETED"].includes(operator?.member_side_quest?.state ?? "") && (
                <MomentScoutCard
                  key={momentScoutResetKey}
                  journeyId={journeyId}
                  onRefreshLocation={onRefreshLocation}
                  onUpdated={loadOperator}
                  sharedRendezvous={groupPresence?.rendezvous
                    ? {
                        placeName: groupPresence.rendezvous.place_name,
                        latitude: groupPresence.rendezvous.latitude,
                        longitude: groupPresence.rendezvous.longitude,
                        coordinateSystem: groupPresence.rendezvous.coordinate_system,
                      }
                    : null}
                />
              )}
            {plannedSeparation && (
              <div className="journey-planned-separation" data-testid="journey-planned-separation" data-state={plannedSeparation.state}>
                {plannedSeparation.members.map((member) => (
                  <div key={`${member.display_name}-${member.rendezvous?.activity_id ?? "rendezvous"}`} data-testid="journey-planned-separation-member">
                    <strong>
                      {member.is_viewer
                        ? (zh ? "你正在按计划暂时单独行动" : "You’re on a planned side trip")
                        : (zh ? `${member.display_name} 正在按计划暂时单独行动` : `${member.display_name} is on a planned side trip`)}
                    </strong>
                    {member.rendezvous && (
                      <span data-testid="journey-planned-separation-rendezvous">
                        {zh
                          ? `在「${member.rendezvous.place_name}」会合${member.rendezvous.scheduled_at ? ` · 计划时间 ${new Date(member.rendezvous.scheduled_at).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}` : ""}`
                          : `Rejoin at ${member.rendezvous.place_name}${member.rendezvous.scheduled_at ? ` · scheduled ${new Date(member.rendezvous.scheduled_at).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}` : ""}`}
                      </span>
                    )}
                    <small data-testid="journey-planned-separation-return-state">
                      {member.return_state === "ON_TRACK"
                        ? (zh ? "返程状态：按当前实时路线可按时会合。" : "Return status: on track on the current provider route.")
                        : member.return_state === "LOW_BUFFER"
                          ? (zh ? "返程缓冲较少，建议尽快返回。" : "Return buffer is low; consider heading back soon.")
                          : member.return_state === "RETURN_RISK"
                            ? (zh ? "可信的当前位置与实时路线显示会合承诺可能受影响。" : "Trusted current location and live route evidence show the rendezvous may be at risk.")
                            : member.return_state === "ADVISORY_RETURN_RISK"
                              ? (zh ? "参考定位路线提示时间可能紧张；这不是可信设备位置。" : "An advisory browser route suggests time may be tight; it is not trusted device location.")
                              : member.return_state === "ADVISORY_ROUTE_ONLY"
                                ? (zh ? "当前只有参考路线；它不代表可信位置，也不会单独触发 Guardian。" : "Only an advisory route is available; it is not trusted location and cannot trigger Guardian by itself.")
                              : (zh ? "返程状态未知：目前没有可靠路线 ETA。" : "Return status unknown: no reliable route ETA is available.")}
                    </small>
                    {member.is_viewer && member.rendezvous && (
                      <MemberSideQuestCard
                        journeyId={journeyId}
                        sideQuest={operator?.member_side_quest ?? null}
                        rendezvous={member.rendezvous}
                        meetupHandshake={groupPresence.meetup_handshake ?? null}
                        placeInvitations={operator?.member_place_invitations}
                        currentSplitActive
                        onRefreshLocation={onRefreshLocation}
                        onHeadBack={routeAfterPrivateStop}
                        onUpdated={loadOperator}
                      />
                    )}
                  </div>
                ))}
                <small data-testid="journey-planned-separation-plan-unchanged">
                  {zh ? "共享行程保持不变；分开行动不等于已经重新会合。" : "The shared Journey is unchanged. Ending a side trip does not itself prove physical reunion."}
                </small>
              </div>
            )}
            {groupPresence.members.map((member) => (
              <small key={member.member_id} data-testid="journey-group-member">
                {member.is_viewer ? (zh ? "你" : "You") : member.display_name}: {member.location_state === "RECENT"
                  ? member.group_state === "SPLIT_FROM_GROUP"
                    ? (zh ? "位置较新 · 可能与队伍分开" : "Recent · may be separated")
                    : member.group_state === "WITH_GROUP"
                      ? (zh ? "位置较新 · 与队伍接近" : "Recent · near the group")
                      : (zh ? "位置较新 · 尚不能判断是否同行" : "Recent · group proximity unknown")
                  : member.location_state === "STALE"
                    ? (zh ? "位置已过时" : "Location is stale")
                    : member.location_state === "IMPRECISE"
                      ? (zh ? "定位精度不足" : "Location is too imprecise")
                      : (zh ? "尚未分享位置" : "No shared location")}
                {member.age_seconds != null && member.location_state === "RECENT" ? ` · ${member.age_seconds}${zh ? "秒前" : "s ago"}` : ""}
                {member.personal_stop && (
                  <span data-testid="journey-member-personal-stop" data-state={member.personal_stop.state}>
                    {member.personal_stop.state === "PLANNED"
                      ? (zh ? " · 已计划一项私人停留（地点仅本人可见，尚未报告出发）" : " · has a planned personal stop (destination visible only to them; departure not reported)")
                      : member.personal_stop.state === "ACTIVE_SELF_REPORTED"
                        ? (zh ? " · 本人已报告开始私人停留（地点仅本人可见）" : " · has reported starting a personal stop (destination visible only to them)")
                        : member.personal_stop.state === "RETURN_STARTED_REPORTED"
                          ? (zh ? " · 本人已报告开始返程（未验证当前位置或实际会合）" : " · has reported heading back (location and reunion are unverified)")
                          : (zh ? " · 私人停留已结束，尚未确认开始返程" : " · personal stop ended; return has not been confirmed")}
                  </span>
                )}
                {member.route_eta_minutes != null ? ` · ${zh ? `到集合点约 ${member.route_eta_minutes} 分钟` : `about ${member.route_eta_minutes} min to rendezvous`}` : ""}
                {member.route_eta_minutes != null && member.route_eta_provenance
                  ? ` · ${member.route_eta_state === "CAPTURED_REPLAY"
                    ? (zh ? `路线来源 ${member.route_eta_provenance}（回放，不代表当前实时路况）` : `route ${member.route_eta_provenance} (replay, not current live traffic)`)
                    : (zh ? `路线来源 ${member.route_eta_provenance}` : `route ${member.route_eta_provenance}`)}`
                  : member.route_eta_state && member.route_eta_state !== "ROUTE_NOT_CAPTURED"
                    ? ` · ${zh ? `路线证据不可用于产品判断：${member.route_eta_state}` : `route evidence unavailable for product decisions: ${member.route_eta_state}`}`
                    : ""}
              </small>
            ))}
            {groupPresence.rendezvous && (
              <small>{zh ? `集合点：${groupPresence.rendezvous.title}（来自当前行程安排）` : `Rendezvous: ${groupPresence.rendezvous.title} (from the active itinerary)`}</small>
            )}
            <small>
              {groupPresence.authority === "TRUSTED_MEMBER_LOCATION"
                ? (zh ? "只显示同行者是否接近，不共享他人的精确坐标；可信位置仍沿正常权限边界处理。" : "Only coarse together/split status is shown; exact coordinates stay private, and trusted location still follows normal authority boundaries.")
                : groupPresence.authority === "MIXED_MEMBER_LOCATION_AUTHORITY"
                  ? (zh ? "部分位置是参考定位、部分来自可信来源；这里只显示粗粒度队伍状态，不共享他人的精确坐标。" : "Location authority is mixed. This view shows only coarse group state and never exposes another traveller’s exact coordinates.")
                  : (zh ? "只显示同行者是否接近，不共享他人的精确坐标。浏览器定位仅用于参考，不会单独触发 Guardian 或改计划。" : "Only coarse together/split status is shown; other travellers’ exact coordinates are never shared. Browser location is advisory and cannot by itself trigger Guardian or change the plan.")}
            </small>
            {groupPresence.can_guide && groupPresence.rendezvous && (
              <button type="button" disabled={busy !== null} data-testid="journey-guide-to-group" onClick={() => void guideToGroup()}>
                {busy === "group-route" ? (zh ? "正在找路线…" : "Finding route…") : (zh ? "带我去集合点" : "Guide me to the group")}
              </button>
            )}
            {groupRoute?.status && (
              <small data-testid="journey-group-route-result">
                {groupRoute.status === "COMPLETED" && groupRoute.route.duration_min != null
                  ? (zh ? `约 ${groupRoute.route.duration_min} 分钟 · ${groupRoute.route.provenance ?? groupRoute.route.provider ?? "路线来源已记录"} · 行程计划未变` : `About ${groupRoute.route.duration_min} min · ${groupRoute.route.provenance ?? groupRoute.route.provider ?? "route source recorded"} · plan unchanged`)
                  : (zh ? "当前没有可靠路线；行程计划未变。" : "No reliable route is available; the plan is unchanged.")}
              </small>
            )}
            {groupRoute?.shared_commitment_risk?.state === "COMMITMENT_AT_RISK" && (
              <small data-testid="journey-group-commitment-risk">
                {zh
                  ? `按这位成员当前路线估算，「${groupRoute.shared_commitment_risk.next_commitment?.title ?? "下一项共同安排"}」可能赶不上。${groupRoute.authority === "ADVISORY_FOREGROUND_LOCATION" ? "浏览器位置只作参考，不会因此触发 Guardian。" : "这是可信成员位置形成的风险事实，已沿现有 Guardian 边界处理。"}`
                  : `This member's current route may miss “${groupRoute.shared_commitment_risk.next_commitment?.title ?? "the next shared commitment"}”. ${groupRoute.authority === "ADVISORY_FOREGROUND_LOCATION" ? "Browser location is advisory and cannot trigger Guardian." : "This risk is grounded in trusted member location and follows the existing Guardian boundary."}`}
              </small>
            )}
            {groupRecovery && showGroupRecovery && (
              <div className="journey-group-recovery" data-testid="journey-group-recovery" data-recovery-state={groupRecovery.state} aria-live="polite">
                <span className="v8-kicker">{recoveryFollowthrough
                  ? (zh ? "恢复后的会合与继续" : "Rejoin and continue")
                  : (zh ? "一起决定怎么继续" : "Choose how the group continues")}</span>
                <strong data-testid="journey-group-recovery-headline">{groupRecovery.headline}</strong>
                {recoveryFollowthrough ? (
                  <div data-testid="group-recovery-followthrough" data-followthrough-state={recoveryFollowthrough.state}>
                    {recoveryFollowthrough.rendezvous && (
                      <small data-testid="group-recovery-rendezvous">
                        {zh
                          ? "新的共同集合点：" + recoveryFollowthrough.rendezvous.title + "（" + recoveryFollowthrough.rendezvous.place_name + "）"
                          : "Shared rendezvous: " + recoveryFollowthrough.rendezvous.title + " (" + recoveryFollowthrough.rendezvous.place_name + ")"}
                      </small>
                    )}
                    <small data-testid="group-recovery-progress-count">
                      {zh
                        ? recoveryFollowthrough.confirmed_count + "/" + recoveryFollowthrough.required_count + " 位仍在旅程中的同行者已确认会合。"
                        : recoveryFollowthrough.confirmed_count + "/" + recoveryFollowthrough.required_count + " current travellers have confirmed rejoining."}
                    </small>
                    {recoveryFollowthrough.members.map((member) => (
                      <small key={member.member_id} data-testid="group-recovery-member-progress">
                        {member.is_viewer ? (zh ? "你" : "You") : member.display_name}: {member.state === "REJOINED"
                          ? (zh ? "已确认会合" : "confirmed rejoining")
                          : member.state === "MOVING"
                            ? (zh ? "正在前往集合点" : "moving to rendezvous")
                            : member.state === "NO_LONGER_REQUIRED"
                            ? (zh ? "已退出或成员资格过期" : "no longer in force")
                            : member.is_viewer
                              ? (zh ? "等待你本人确认" : "waiting for your confirmation")
                              : (zh ? "等待本人确认" : "waiting for their own confirmation")}
                      </small>
                    ))}
                    {recoveryFollowthrough.membership_reconfirmation_required && (
                      <small data-testid="group-recovery-membership-change">
                        {zh
                          ? "成员或角色在恢复过程中发生变化；为避免漏掉同行者，系统不会自动关闭这次恢复。"
                          : "Membership or role changed during recovery. CityNexus will not close it while a traveller may be missing."}
                      </small>
                    )}
                    {recoveryFollowthrough.evidence_level === "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH" && (
                      <small data-testid="group-recovery-evidence-boundary">
                        {zh
                          ? (recoveryFollowthrough.physical_presence_verified
                            ? "每位同行者通过自己的账号完成到达确认；本次还包含设备验证的位置证据。"
                            : "每位同行者通过自己的账号完成行程到达确认；路线、预计时间或普通浏览器定位不会被当成到场证明。")
                          : "Each traveller uses their own authenticated Journey arrival action. ETA, route data, and ordinary browser location are not proof of presence."}
                      </small>
                    )}
                    {recoveryFollowthrough.viewer_has_confirmed && recoveryFollowthrough.state !== "RECOVERY_CLOSED" && (
                      <small data-testid="group-recovery-viewer-waiting">
                        {zh ? "你的确认已保存；正在等其他同行者本人确认。" : "Your confirmation is saved; waiting for the other travellers to confirm for themselves."}
                      </small>
                    )}
                    {recoveryFollowthrough.viewer_can_confirm && (
                      <button
                        type="button"
                        disabled={busy !== null}
                        data-testid="group-recovery-confirm-rejoin"
                        onClick={() => void confirmGroupRejoin()}
                      >
                        {busy === "group-recovery-rejoin"
                          ? (zh ? "正在记录…" : "Saving…")
                          : (zh ? "我已到达集合点" : "I’ve reached the rendezvous")}
                      </button>
                    )}
                    {recoveryFollowthrough.state === "RECOVERY_CLOSED" && (
                      <small data-testid="group-recovery-closed">
                        {zh
                          ? "这次恢复已关闭，原行程计划没有因关闭动作再次变化；下面继续显示当前 Journey。"
                          : "This recovery is closed. Closing it did not change the active plan; the current Journey continues below."}
                      </small>
                    )}
                  </div>
                ) : (
                  <>
                {groupRecovery.old_decision && groupRecovery.state.startsWith("RECOVERY_REGROUND") && (
                  <small data-testid="journey-group-recovery-old-decision">
                    {zh
                      ? "之前的共同选择仍保留在历史中，但旧方案和旧回答已不能用于当前行程；当前 Journey 没有改变。"
                      : "The earlier group choice remains in history, but its proposal and answers cannot be reused for the current trip. The Journey is unchanged."}
                  </small>
                )}
                {groupRecovery.state === "RECOVERY_REPLAN_REQUIRED" && (
                  <small data-testid="journey-group-recovery-replan-required">
                    {zh
                      ? "旧决定已停止生效；当前状态没有允许自动重新规划，因此不会重复询问或套用旧方案。"
                      : "The old decision is no longer spendable. Current conditions do not permit an automatic re-ground, so no old option or answer will be reused."}
                  </small>
                )}
                {groupRecovery.state === "RECOVERY_REGROUNDING" && (
                  <small data-testid="journey-group-recovery-regrounding">
                    {zh
                      ? "CityNexus 正在按当前行程和当前事实重新找方案；这一步不会应用计划。"
                      : "CityNexus is checking the current Journey and current evidence for new options. This planning step cannot apply a plan."}
                  </small>
                )}
                {groupRecovery.state === "RECOVERY_REGROUND_NO_FEASIBLE_OPTION" && (
                  <small data-testid="journey-group-recovery-no-feasible">
                    {zh
                      ? "暂时找不到满足当前保护承诺与现实证据的共享调整；大家仍可按现有集合指引继续会合。"
                      : "No shared adjustment currently satisfies the protected commitments and available evidence. The existing rendezvous guidance remains available."}
                  </small>
                )}
                {groupRecovery.reground_result?.state === "RECOVERY_REGROUNDED_DOMINANT" && (
                  <small data-testid="journey-group-recovery-regrounded-option">
                    {zh
                      ? `已按当前现实重新核验出 ${groupRecovery.reground_result.grounded_feasible_proposal_ids?.length ?? 0} 个可行共享方案${groupRecovery.reground_result.current_options?.flatMap((option) => option.place_names).length ? `：${groupRecovery.reground_result.current_options?.flatMap((option) => option.place_names).join("、")}` : ""}。行程尚未改变；这只是新方案，不会自动应用，后续仍走现有集体授权。`
                      : `${groupRecovery.reground_result.grounded_feasible_proposal_ids?.length ?? 0} grounded shared option(s) were rechecked against the current trip${groupRecovery.reground_result.current_options?.flatMap((option) => option.place_names).length ? `: ${groupRecovery.reground_result.current_options?.flatMap((option) => option.place_names).join(", ")}` : ""}. The Journey is unchanged; this is a proposal, not an automatic plan change, and existing collective authority still applies.`}
                  </small>
                )}
                {groupRecovery.affected_member && groupRecovery.shared_commitment && (
                  <small data-testid="journey-group-recovery-risk">
                    {zh
                      ? `${groupRecovery.affected_member.display_name} 到「${groupRecovery.shared_commitment.title}」约需 ${groupRecovery.affected_member.eta_minutes} 分钟；安全缓冲 ${groupRecovery.shared_commitment.slack_after_buffer_min ?? "未知"} 分钟。`
                      : `${groupRecovery.affected_member.display_name} needs about ${groupRecovery.affected_member.eta_minutes} min to reach “${groupRecovery.shared_commitment.title}”; buffer after the safety margin: ${groupRecovery.shared_commitment.slack_after_buffer_min ?? "unknown"} min.`}
                    {groupRecovery.affected_member.route_evidence_state === "CAPTURED_REPLAY"
                      ? (zh ? " 这条路线是回放证据，不是当前实时路况。" : " This is replay route evidence, not current live traffic.")
                      : ""}
                  </small>
                )}
                {groupRecovery.options.some((option) => option.kind === "KEEP_PLAN_MEMBER_CATCH_UP") && (
                  <small data-testid="journey-group-recovery-keep-plan">
                    {zh
                      ? "保持当前行程：其他人的安排不变；这位同行者按集合路线赶上。"
                      : "Keep the plan: nobody else’s itinerary changes; this traveller follows the rendezvous route and catches up."}
                  </small>
                )}
                {groupRecovery.options.some((option) => option.mutates_plan) && (
                  <small data-testid="journey-group-recovery-adjust-impact">
                    {zh
                      ? "一起调整：CityNexus 只会先准备候选方案。任何改变全组行程的方案仍须经过现有同行者与审批权限。"
                      : "Adjust together: CityNexus will only prepare a proposal. Any shared-plan change still follows the existing collective and approval rules."}
                  </small>
                )}
                {groupRecovery.mission && (
                  <small data-testid="journey-group-recovery-agent-status">
                    {groupRecovery.mission.status === "FAILED_EXTERNAL"
                      ? (zh ? "Agent 暂时不可用；没有自动改动行程。" : "The Agent could not start; no trip change was made.")
                      : groupRecovery.mission.status === "RUNNING"
                        ? (zh ? "CityNexus 正在准备方案；原行程仍然有效。" : "CityNexus is preparing options; the current plan remains active.")
                        : (zh ? `恢复任务状态：${groupRecovery.mission.status}` : `Recovery task: ${groupRecovery.mission.status}`)}
                  </small>
                )}
                {groupRecovery.collective_decision && groupRecovery.state !== "RECOVERY_APPLIED" && (
                  <small data-testid="journey-group-recovery-collective-state">
                    {groupRecovery.collective_decision.state === "WAITING_FOR_MEMBERS"
                      ? (zh
                        ? `共享恢复选择已开放：${groupRecovery.collective_decision.response_count}/${groupRecovery.collective_decision.required_count} 位相关同行者已回答。${groupRecovery.collective_decision.viewer_has_responded ? "你的选择已记录，正在等其他人。" : "请在下方共享决策区只提交你自己的选择。"} 行程尚未改变。`
                        : `The shared recovery choice is open: ${groupRecovery.collective_decision.response_count}/${groupRecovery.collective_decision.required_count} affected travellers have answered. ${groupRecovery.collective_decision.viewer_has_responded ? "Your choice is recorded; CityNexus is waiting for the others." : "Choose only for yourself in the shared decision room below."} The Journey has not changed.`)
                      : groupRecovery.collective_decision.state === "MEDIATING"
                        ? (zh ? "同行者的选择有分歧；CityNexus 保留分歧并继续寻找可接受的下一步，尚未修改行程。" : "The travellers disagree. CityNexus is preserving that disagreement while looking for an acceptable next step; the Journey is unchanged.")
                        : groupRecovery.collective_decision.state === "COLLECTIVE_AUTHORIZED"
                          ? (zh
                            ? `服务器已为“${groupRecovery.collective_decision.selected_option_label ?? "选定方案"}”记录当前集体授权；这不等于方案已应用。`
                            : `The server has current group authorization for “${groupRecovery.collective_decision.selected_option_label ?? "the selected option"}”; it has not been applied yet.`)
                          : groupRecovery.collective_decision.state === "COLLECTIVE_STALE"
                            ? (zh ? "行程、成员或方案事实已经变化，旧选择按钮已失效；需要重新计算。" : "The trip, membership, or proposal facts changed. Old choices are no longer valid and must be recomputed.")
                            : (zh ? "成员回答已记录，但当前没有可用于推进此方案的集体授权。行程尚未改变。" : "Member responses are recorded, but there is no current collective authority to carry this proposal forward. The Journey is unchanged.")}
                  </small>
                )}
                {groupRecovery.collective_decision?.reconsent_required && (
                  <small data-testid="journey-group-recovery-reconsent">
                    {zh
                      ? "之前的共同选择因行程或事实变化而过期；现在是新一轮决定，旧回答不会自动沿用。"
                      : "The previous shared choice expired after the Journey or its facts changed. This is a fresh decision round; earlier answers do not carry over."}
                  </small>
                )}
                {groupRecovery.collective_decision?.state === "COLLECTIVE_AUTHORIZED"
                  && groupRecovery.collective_decision.viewer_can_apply && (
                  <button
                    type="button"
                    className="v8-primary-button"
                    disabled={busy !== null}
                    data-testid="journey-group-recovery-apply"
                    onClick={() => void applySharedRecovery()}
                  >
                    {busy === "group-recovery-apply"
                      ? (zh ? "正在应用共同决定…" : "Applying the group decision…")
                      : (zh ? "应用共同恢复" : "Apply shared recovery")}
                  </button>
                )}
                {groupRecovery.collective_decision?.state === "COLLECTIVE_AUTHORIZED"
                  && !groupRecovery.collective_decision.viewer_can_apply && (
                  <small data-testid="journey-group-recovery-apply-waiting">
                    {zh ? "共同选择已达成，正在等待有行程编辑权限的成员应用。" : "The group has agreed; waiting for a traveller with Journey editing authority to apply it."}
                  </small>
                )}
                {groupRecoveryResult?.status === "RECOVERY_EXTERNAL_AGENT_BLOCKED" && !groupRecovery.mission && (
                  <small data-testid="journey-group-recovery-agent-status">{zh ? "Agent 暂时不可用；可以继续按当前集合路线会合。" : "The Agent is unavailable; you can still follow the current rendezvous route."}</small>
                )}
                {groupRecovery.can_request_planning && (
                  <button
                    type="button"
                    disabled={busy !== null}
                    data-testid="journey-group-recovery-adjust"
                    onClick={() => void requestGroupRecovery()}
                  >
                    {busy === "group-recovery"
                      ? (zh ? "正在准备全组方案…" : "Preparing a group option…")
                      : (zh ? "帮我们一起调整" : "Adjust together")}
                  </button>
                )}
                  </>
                )}
              </div>
            )}
          </section>
        )}
        {operator?.member_side_quest && !plannedSeparation && (
          <MemberSideQuestCard
            journeyId={journeyId}
            sideQuest={operator.member_side_quest}
            rendezvous={operator.member_side_quest.rendezvous}
            meetupHandshake={groupPresence?.meetup_handshake ?? null}
            placeInvitations={operator?.member_place_invitations}
            currentSplitActive={false}
            onUpdated={loadOperator}
          />
        )}
        {operator?.progress?.basis && operator.progress.state !== "UNKNOWN_PROGRESS" && (
          <div className={`journey-progress journey-progress-${operator.progress.state.toLowerCase()}`} data-testid="journey-progress" aria-live="polite">
            <span className="v8-kicker">{zh ? "行程进度" : "Trip progress"}</span>
            <strong data-testid="journey-progress-headline">{operator.progress.headline}</strong>
            {operator.progress.next_risk && operator.progress.next_risk.slack_after_min != null && (
              <small data-testid="journey-progress-slack">
                {zh
                  ? `${operator.progress.next_risk.activity_title} 剩余 ${operator.progress.next_risk.slack_after_min} 分钟缓冲`
                  : `${operator.progress.next_risk.activity_title} has ${operator.progress.next_risk.slack_after_min} min of remaining slack`}
              </small>
            )}
          </div>
        )}
        {operator?.ahead && (
          <div className={`journey-ahead journey-ahead-${operator.ahead.state.toLowerCase()}`} data-testid="journey-ahead" aria-live="polite">
            <span className="v8-kicker">{zh ? "同时看着后面的行程" : "Looking ahead"}</span>
            <strong>{operator.ahead.headline}</strong>
            <small>
              {zh
                ? `未来 ${operator.ahead.horizon.remaining_activity_count} 个行程点 · 看到 ${new Date(operator.ahead.horizon.lookahead_until).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}`
                : `${operator.ahead.horizon.remaining_activity_count} future stops · looking through ${new Date(operator.ahead.horizon.lookahead_until).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}`}
            </small>
            {operator.ahead.top_risk && (
              <span data-testid="journey-ahead-risk">
                {zh
                  ? `${operator.ahead.top_risk.title ?? "后续行程"} · ${operator.ahead.top_risk.lead_time_minutes ?? ""} 分钟后${operator.ahead.top_risk.grounded ? " · 有依据的预测" : " · 继续观察"}`
                  : `${operator.ahead.top_risk.title ?? "Later stop"} · in ${operator.ahead.top_risk.lead_time_minutes ?? ""} min${operator.ahead.top_risk.grounded ? " · grounded forecast" : " · watching"}`}
              </span>
            )}
          </div>
        )}
        {operator?.overnight && operator.overnight.state !== "INACTIVE" && (
          <div className={`journey-overnight journey-overnight-${operator.overnight.state.toLowerCase()}`} data-testid="journey-overnight">
            <span className="v8-kicker">{zh ? "今晚我继续照看行程" : "I’ll keep looking after your trip overnight"}</span>
            <strong>
              {operator.overnight.state === "MORNING_READY"
                ? (zh ? "早上好，你今天的行程已准备好。" : "Good morning — today’s trip is prepared.")
                : operator.overnight.state === "MORNING_NEEDS_ATTENTION"
                  ? (zh ? "明天的第一站已准备好，出发路线还需要确认。" : "Your first stop is ready, but I still need a reliable route before I can tell you when to leave.")
                  : (zh ? "我会继续照看明天的安排。" : "I’m watching tomorrow’s plan while you rest.")}
            </strong>
            {operator.overnight.learning_summary?.summary && (
              <small data-testid="journey-overnight-learning">{operator.overnight.learning_summary.summary}</small>
            )}
            {operator.overnight.briefing?.first_activities?.slice(0, 3).map((item, index) => (
              <span key={item.id}>{index === 0 ? (zh ? `第一站：${item.title}` : `First stop: ${item.title}`) : (zh ? `之后：${item.title}` : `Then: ${item.title}`)}</span>
            ))}
            {operator.overnight.briefing?.route?.exact && operator.overnight.briefing.route.must_leave_by && (
              <small>{zh ? `建议 ${new Date(operator.overnight.briefing.route.must_leave_by).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })} 出发 · ${operator.overnight.briefing.route.duration_min} 分钟路线` : `Leave around ${new Date(operator.overnight.briefing.route.must_leave_by).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })} · ${operator.overnight.briefing.route.duration_min} min route`}</small>
            )}
            {operator.overnight.briefing?.future_risk && (
              <small>{zh ? `留意后续行程：${operator.overnight.briefing.future_risk.title ?? "有依据的风险"}` : `One thing to watch: ${operator.overnight.briefing.future_risk.title ?? "a grounded future risk"}`}</small>
            )}
          </div>
        )}
        {operator?.brain && (
          <div className="journey-operator-brain" data-testid="journey-operator-brain">
            <span className="v8-kicker">Planning brain</span>
            <strong>{operator.brain.headline}</strong>
            {operator.brain.recovery_resume.pending && (
              <span>
                {operator.brain.recovery_resume.safe_to_resume
                  ? (zh ? "问题已记住，规划 AI 恢复后会继续处理。" : "I remembered the problem and will continue when planning is available.")
                  : (zh ? "这次恢复已保留，需要安全地回到原来的处理链。" : "I kept this recovery safe and will not switch brains blindly.")}
              </span>
            )}
          </div>
        )}
        {operator?.collective && operator.collective.state !== "SINGLE_TRAVELLER" && (
          <div className="journey-operator-collective" data-testid="journey-operator-collective">
            <span className="v8-kicker">Shared trip</span>
            <strong>
              {operator.collective.state === "MEDIATING"
                ? (zh ? "大家的选择不同，CityNexus 正在寻找兼顾彼此的方案。" : "Your choices differ. CityNexus is looking for a compromise that protects everyone.")
                : operator.collective.state === "COLLECTIVE_AUTHORIZED"
                  ? (zh ? "服务器已记录此方案所需的集体授权；行程仍须经过正常审批与接受边界。" : "The server recorded collective authority for this proposal; normal approval and acceptance boundaries still apply.")
                  : operator.collective.state === "COLLECTIVE_STALE"
                    ? (zh ? "行程情况已变化，需要重新确认。" : "The trip changed, so this group decision needs to be renewed.")
                    : operator.collective.state === "WAITING_FOR_MEMBERS"
                      ? (zh ? `共享恢复方案正在等同行者决定：${operator.collective.response_count}/${operator.collective.required_count} 已回答；行程尚未改变。` : `The shared recovery is waiting for travellers: ${operator.collective.response_count}/${operator.collective.required_count} have answered; the Journey is unchanged.`)
                      : operator.collective.state === "COLLECTIVE_READY"
                        ? (zh ? "相关同行者都已回答；服务器仍在核对当前权限，行程没有自动改变。" : "All required travellers have answered; the server is still checking current authority. The Journey has not changed automatically.")
                    : (zh
                      ? `这次变化影响 ${operator.collective.required_count} 位同行者，已回答 ${operator.collective.response_count} 位。`
                      : `This change affects ${operator.collective.required_count} travellers; ${operator.collective.response_count} have responded.`)}
            </strong>
            <div className="journey-operator-members">
              {operator.collective.members.map((member) => (
                <span key={member.member_id ?? member.display_name} data-responded={member.responded}>
                  {member.display_name}{member.responded ? (zh ? " · 已回答" : " · answered") : (zh ? " · 等待中" : " · waiting")}
                </span>
              ))}
            </div>
            {operator.collective.viewer_can_respond && operator.collective.options.length > 0 && operator?.attention?.action_type !== "RESPOND_COLLECTIVE" && (
              <div className="journey-operator-collective-options">
                <span>{zh ? "你更愿意哪种处理？" : "Which recovery fits you?"}</span>
                {operator.collective.options.map((option) => (
                  <button
                    key={option.proposal_id}
                    type="button"
                    disabled={busy !== null}
                    onClick={() => void answerCollectiveOption(option.proposal_id)}
                  >
                    {option.label}
                    {option.schedule_delta_min != null ? ` · ${option.schedule_delta_min} min` : ""}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {operator?.discovery && operator.discovery.state !== "IDLE" && (
          <div className="journey-operator-discovery" data-testid="journey-operator-discovery">
            <span className="v8-kicker">Open-world recovery</span>
            <strong>
              {operator.discovery.state === "DISCOVERY_IN_PROGRESS"
                ? (zh ? "已知选择不够，我正在附近寻找新的可能性。" : "The known choices are too narrow, so I’m looking nearby for new possibilities.")
                : operator.discovery.state === "DISCOVERY_DEGRADED"
                  ? (zh ? "暂时无法连接城市发现服务，已有行程选项保持不变。" : "City discovery is temporarily unavailable; your existing recovery options are unchanged.")
                  : (zh ? "我已经找过附近的替代方案，但还没有更多可靠选择。" : "I looked nearby, but I still don’t have another grounded recovery.")}
            </strong>
          </div>
        )}
        {operator?.operations && (
          <div id="journey-operator-operations" className="journey-operator-operations" data-testid="journey-operator-operations">
            <span className="v8-kicker">{zh ? "外部事务" : "Trip operations"}</span>
            <strong>
              {operator.operations.status === "WAITING_FOR_APPROVAL"
                ? (zh ? "这次调整还需要处理预订。" : "This recovery still needs reservation work.")
                : operator.operations.status === "EXECUTING"
                  ? (zh ? "正在更新行程并处理预订……" : "Updating the trip and handling the reservation…")
                  : operator.operations.status === "COMPLETED"
                    ? (zh ? "Sandbox 预订已确认，继续照看行程。" : "Sandbox booking confirmed. I’m watching the trip again.")
                    : operator.operations.status === "PARTIALLY_APPLIED"
                      ? (zh ? "部分预订操作已完成，剩余事项需要你的注意。" : "Part of the reservation work completed; the remaining work needs your attention.")
                      : (zh ? "预订操作需要你的注意。" : "Reservation work needs your attention.")}
            </strong>
            <span>
              {zh
                ? `${operator.operations.completed_action_count} / ${operator.operations.planned_action_count} 项已完成 · ${operator.operations.provider_label ?? "LAB_SANDBOX_PROVIDER"}`
                : `${operator.operations.completed_action_count} of ${operator.operations.planned_action_count} actions complete · ${operator.operations.provider_label ?? "LAB_SANDBOX_PROVIDER"}`}
            </span>
          </div>
        )}
        {!operator?.enabled && (
          <button
            type="button"
            data-testid="journey-operator-start"
            disabled={busy !== null}
            onClick={() => void startOperator()}
          >
            {zh ? "照看这趟行程" : "Take care of this trip"}
          </button>
        )}
        {operator?.enabled && <span data-testid="journey-operator-state">
          {operator.state === "WATCHING"
            ? (zh ? "正在照看" : "Watching")
            : operator.state === "INVESTIGATING"
              ? (zh ? "正在了解情况" : "Working on a recovery")
              : operator.state === "WAITING_FOR_APPROVAL"
                ? (zh ? "等待你的批准" : "Waiting for your approval")
                : operator.state === "RECOVERED"
                  ? (zh ? "调整已应用，继续照看" : "Change applied · watching again")
                  : operator.state === "NEEDS_ATTENTION"
                    ? (zh ? "需要你的注意" : "Needs your attention")
                    : operator.state === "DEGRADED"
                      ? (zh ? "暂时无法完整照看" : "Needs attention")
                      : (zh ? "正在处理" : "Working")}
        </span>}
        {operator?.enabled && (
          <div className="journey-intent-steering" data-testid="journey-inbox-intake">
            <label htmlFor="journey-inbox-input">
              <strong>{zh ? "有预订信息变更？" : "Booking update"}</strong>
              <span>{zh ? "粘贴确认信息、时间变更或取消通知，CityNexus 会先核对对应预订。" : "Paste a confirmation, schedule change, or cancellation. CityNexus will match the reservation first."}</span>
            </label>
            <textarea id="journey-inbox-input" data-testid="journey-inbox-input" rows={2} maxLength={1200}
              value={inboxText} onChange={(event) => setInboxText(event.target.value)}
              placeholder={zh ? "例如：Booking ZX42 update: departure changed from 17:40 to 18:10. Confirmation AB123." : "Example: Booking ZX42 update: departure changed from 17:40 to 18:10. Confirmation AB123."} />
            <button type="button" data-testid="journey-inbox-submit" disabled={busy !== null || !inboxText.trim()} onClick={() => void submitJourneyInbox()}>
              {busy === "journey-inbox" ? (zh ? "正在核对…" : "Checking…") : (zh ? "提交预订变更" : "Add booking update")}
            </button>
            {inboxResult && <span role="status" data-testid="journey-inbox-result">{inboxResult}</span>}
          </div>
        )}
        {operator?.enabled && (
          <div className="journey-intent-steering" data-testid="journey-intent-steering">
            <label htmlFor="journey-intent-input">
              <strong>{zh ? "想调整行程，或暂时和同行者分开行动？" : "Want to steer the Journey or split off briefly?"}</strong>
              <span>{zh ? "告诉 CityNexus 你的临时安排；分开行动会绑定到服务器确认的下一处共同会合点。" : "Tell CityNexus your temporary plan. A side trip stays tied to a server-resolved shared rendezvous."}</span>
            </label>
            <textarea
              id="journey-intent-input"
              data-testid="journey-intent-input"
              rows={2}
              maxLength={500}
              value={intentText}
              onChange={(event) => setIntentText(event.target.value)}
              placeholder={zh ? "比如：我想一个人逛一会儿，下一处共同安排时和大家会合。" : "For example: I’d like to explore on my own for a while and meet everyone at the next shared stop."}
            />
            <button
              type="button"
              data-testid="journey-intent-submit"
              disabled={busy !== null || !intentText.trim()}
              onClick={() => void submitJourneyIntent()}
            >
              {busy === "journey-intent" ? (zh ? "正在调整…" : "Steering…") : (zh ? "调整接下来的行程" : "Steer my Journey")}
            </button>
            {intentResult && <span role="status" data-testid="journey-intent-result">{intentResult}</span>}
            {!!operator.intents?.length && (
              <ul className="journey-intent-list" data-testid="journey-intent-list">
                {operator.intents.map((intent) => (
                  <li key={intent.id}>
                    <span>{intent.source_text}</span>
                    <small>{intent.scope_kind?.replaceAll("_", " ") ?? (zh ? "临时行程意图" : "Temporary Journey intent")} · {intent.evaluation_state.replaceAll("_", " ")}</small>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {operator?.approval?.required && operator.decision && operator?.attention?.action_type !== "REVIEW_APPROVAL" && (
          <div className="journey-operator-approval" data-testid="journey-operator-approval">
            <b>{zh ? "CityNexus 找到一个更稳妥的调整" : "CityNexus recommends this change"}</b>
            <span>{operator.decision.summary ?? (zh ? "这项调整可以减少行程影响。" : "This change should reduce the impact on your trip.")}</span>
            {operator.decision.schedule_delta_min != null && (
              <span>{zh ? `时间影响：${operator.decision.schedule_delta_min} 分钟` : `Schedule impact: ${operator.decision.schedule_delta_min} min`}</span>
            )}
            <button
              type="button"
              data-testid="journey-operator-approve"
              disabled={busy !== null}
              onClick={() => void approveOperatorChange()}
            >
              {zh ? "批准变更" : "Approve change"}
            </button>
            <button
              type="button"
              data-testid="journey-operator-reject"
              disabled={busy !== null}
              onClick={() => void rejectOperatorChange()}
            >
              {zh ? "保留当前行程" : "Keep current plan"}
            </button>
          </div>
        )}
        {operator?.timeline?.length ? (
          <ol data-testid="journey-operator-timeline">
            {operator.timeline.slice(-5).map((item) => <li key={`${item.at}-${item.kind}`}>{item.label}</li>)}
          </ol>
        ) : null}
      </section>

      {guardianSurfaceState && (
        <div
          className={`live-guardian-check live-guardian-check-${guardianSurfaceState}`}
          data-testid="live-guardian-check"
          data-state={guardianSurfaceState}
          role="status"
        >
          <b>
            {guardianSurfaceState === "clear"
              ? zh
                ? "刚刚检查过"
                : "Just checked"
              : guardianSurfaceState === "incomplete"
                ? zh
                  ? "部分信息待确认"
                  : "Some information needs checking"
                : zh
                  ? "正在处理行程变化"
                  : "Trip change is being handled"}
          </b>
          <span>
            {guardianSurfaceState === "clear"
              ? t("live.guardianCheckedClear")
              : guardianSurfaceState === "incomplete"
                ? t("live.guardianVerificationIncomplete")
                : t("live.guardianRecoveryActive")}
          </span>
          {guardianPulse?.completed_at && (
            <time dateTime={guardianPulse.completed_at}>
              {new Date(guardianPulse.completed_at).toLocaleTimeString(locale, {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </time>
          )}
        </div>
      )}

      {guardianWatch?.enabled && guardianWatch.next_move?.copy && (
        <div className="live-next-move" data-testid="live-next-move" role="status">
          <b>{zh ? "下一步" : "Next move"}</b>
          <span>{guardianWatch.next_move.copy}</span>
        </div>
      )}

      {launchSetup?.status === "NEEDS_ORIGIN" && (
        <div className="live-readiness live-launch-setup" data-testid="live-launch-setup" role="status">
          <b>{zh ? "你将从哪里出发？" : "Where will you start from?"}</b>
          <span>
            {zh
              ? "CityNexus 需要一个明确的起点，才能估算何时出发。"
              : "CityNexus needs a starting point before it can estimate when to leave."}
          </span>
          {(launchSetup.candidates ?? []).filter((candidate) => candidate.eligible && candidate.display_name).map((candidate) => (
            <button
              key={candidate.kind}
              type="button"
              disabled={busy !== null}
              onClick={() => {
                if (candidate.kind === "SELECTED_STAY") {
                  void confirmLaunchSource("SELECTED_STAY");
                } else if (candidate.kind === "ARRIVAL_ANCHOR") {
                  void confirmLaunchSource("ARRIVAL_ANCHOR");
                }
              }}
            >
              {zh ? `从 ${candidate.display_name} 出发` : `Start from ${candidate.display_name}`}
            </button>
          ))}
          {(launchSetup.actions ?? []).includes("CHOOSE_PLACE") && (
            <small>{zh ? "也可以选择一个已验证的地点作为计划起点。" : "You can also choose another verified place as your planned starting point."}</small>
          )}
        </div>
      )}

      {launchSetup?.contract?.display_name && (
        <div className="live-next-move" data-testid="live-launch-confirmed" role="status">
          {zh
            ? `首段路线将从 ${launchSetup.contract.display_name} 规划。`
            : `Planning your first route from ${launchSetup.contract.display_name}.`}
          {!routeOperation || routeOperation.status === "UNAVAILABLE" || routeOperation.status === "FAILED" ? (
            <button type="button" disabled={busy !== null} onClick={() => void checkRoute()}>
              {zh ? "检查路线" : "Check route"}
            </button>
          ) : null}
          {routeOperation?.status === "RUNNING" && (
            <small>{zh ? "正在检查从起点到下一站的路线……" : "Checking the route to your first stop…"}</small>
          )}
          {routeOperation?.status === "COMPLETED" && routeOperation.result?.duration_min != null && (
            <small>
              {zh
                ? `约 ${routeOperation.result.duration_min} 分钟${leaveByDisplay ? ` · 建议 ${leaveByDisplay} 前出发` : ""}`
                : `About ${routeOperation.result.duration_min} min${leaveByDisplay ? ` · leave around ${leaveByDisplay}` : ""}`}
            </small>
          )}
          {routeOperation?.status === "UNAVAILABLE" && (
            <small>{zh ? "CityNexus 暂时无法获取这段路线的可靠结果。" : "CityNexus could not get a reliable route for this leg."}</small>
          )}
          {routeOperation?.status === "FAILED" && (
            <small>{zh ? "路线服务暂时不可用。" : "The route provider is unavailable right now."}</small>
          )}
        </div>
      )}

      {departureReadiness && (
        <div
          className={`live-readiness live-readiness-${String(departureReadiness.state ?? "unknown").toLowerCase()}`}
          data-testid="live-departure-readiness"
          role="status"
        >
          <b>{zh ? "下一站准备情况" : "Ready for next?"}</b>
          {departureReadiness.target_title && <strong>{departureReadiness.target_title}</strong>}
          <span>
            {departureReadiness.state === "READY"
              ? zh ? "可以出发 · 下一站按计划开放。" : "Ready to go · next stop opens as planned."
              : departureReadiness.state === "BLOCKED"
                ? zh ? "先不要出发 · 下一站目前无法前往。" : "Do not leave yet · the next stop is currently unavailable."
                : departureReadiness.state === "ACTION_NEEDED"
                  ? zh ? "出发前检查 · 下一站或路线仍需确认。" : "Check before leaving · the next stop or route still needs checking."
                  : departureReadiness.state === "NOT_DUE"
                    ? zh ? "还没到出发时间 · 按计划准备即可。" : "Not time to leave yet · stay with the plan."
                    : zh ? "基本准备好了 · 仍有实时信息未确认。" : "Mostly ready · some live information is unavailable."}
          </span>
          {departureReadiness.must_leave_by && (
            <small>
              {zh ? "建议在 " : "Leave by "}
              {new Date(departureReadiness.must_leave_by).toLocaleTimeString(locale, {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </small>
          )}
          {departureReadiness.launch_origin?.name && (
            <small>
              {zh ? `出发点：${departureReadiness.launch_origin.name}` : `Starting from ${departureReadiness.launch_origin.name}`}
            </small>
          )}
          {(() => {
            const activation = departureReadiness.departure_activation;
            if (!activation) return null;
            if (activation.state === "ALREADY_EN_ROUTE") {
              return <strong>{zh ? "正在前往下一站。" : "On the way to the next stop."}</strong>;
            }
            if (activation.state === "BLOCKED") {
              return <strong>{zh ? "先暂停出发，请先检查行程提示。" : "Hold before leaving — check the issue first."}</strong>;
            }
            if (activation.state === "NO_ROUTE") {
              return <small>{zh ? "路线尚未准备好。" : "The route is not ready yet."}</small>;
            }
            if (activation.state === "NOT_DUE") return null;
            return (
              <>
                <strong>
                  {activation.state === "LEAVE_SOON"
                    ? zh ? "快到出发时间了。" : "You’ll need to leave soon."
                    : activation.state === "OVERDUE"
                      ? zh ? "已超过建议出发时间；确认你已在路上。" : "You’re past the planned leave time; confirm when you’re on the way."
                      : zh ? `该出发前往 ${activation.target_title ?? departureReadiness.target_title ?? "下一站"} 了。` : `It’s time to leave for ${activation.target_title ?? departureReadiness.target_title ?? "your next stop"}.`}
                </strong>
                {activation.can_confirm_departure === true && (
                  <button type="button" disabled={busy !== null} onClick={() => void confirmDeparture()}>
                    {zh ? "我出发了" : "I’m leaving"}
                  </button>
                )}
              </>
            );
          })()}
        </div>
      )}

      {journeyCompleted && (openDisruptions.length > 0 || pending.length > 0) && (
        <p className="v8-muted" data-testid="live-completed">
          {t("live.completedProposalIntro")}
        </p>
      )}

      {openDisruptions.length > 0 && (
        <div className="live-disruption" data-testid="live-disruption" role="status">
          <b>
            {journeyCompleted
              ? t("live.completedDisruption")
              : zh
                ? "发生了什么"
                : "What happened"}
          </b>
          <ul>
            {openDisruptions.slice(0, 3).map((d) => {
              const copy = EVENT_COPY[d.type] ?? EVENT_COPY.OTHER;
              const affected = activities.find((a) => a.id === d.affected_activity_id);
              return (
                <li key={d.id}>
                  {zh ? copy.zh : copy.en}
                  {affected ? (
                    <span className="live-affected" data-testid="live-affected">
                      {zh ? " · 受影响：" : " · Affected: "}
                      {String(affected.title ?? "")}
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* acknowledged-only events are a subset of openDisruptions (an
          acknowledged event is still open), so this must key off their own
          count — the earlier `openDisruptions.length === 0` guard made this
          branch unreachable (V2-T3). */}
      {acknowledgedOnly.length > 0 && (
        <p className="v8-muted" data-testid="live-acknowledged">
          {zh
            ? "已知晓，仍待处理：你已经看过这些变化，但还没有选定新的安排。"
            : "Seen, still open: you have seen these changes but have not chosen a new arrangement yet."}
        </p>
      )}

      {duplicateCount > 0 && (
        <p className="v8-muted" data-testid="live-deduped">
          {zh
            ? `已合并 ${duplicateCount} 个内容相同的建议。`
            : `Merged ${duplicateCount} identical suggestion(s).`}
        </p>
      )}

      {individuallyActionablePending.length > 0 ? (
        individuallyActionablePending.map((proposal, index) => {
          const removed = proposal.diff?.removed ?? [];
          const added = proposal.diff?.added ?? [];
          const moved = proposal.diff?.moved ?? [];
          const optionLabel = zh
            ? `方案 ${String.fromCharCode(65 + index)}`
            : `Option ${String.fromCharCode(65 + index)}`;
          const summary = proposal.explanation?.summary?.trim();
          return (
            <article
              className="live-proposal"
              key={proposal.id}
              data-testid="live-proposal"
              data-authority={journeyCompleted ? "advisory-after-completion" : "actionable"}
              data-state={busy === proposal.id ? "accepting" : "idle"}
            >
              <b data-testid="live-proposal-heading">
                {journeyCompleted
                  ? t("live.completedProposalTitle")
                  : `${optionLabel} · ${zh ? "推荐怎么改" : "Suggested change"}`}
              </b>
              {summary && (
                <p className="live-summary" data-testid="live-summary">
                  {summary}
                </p>
              )}
              {proposal.source_agent_run_id && (
                <p
                  className="v8-muted"
                  data-testid="live-agent-origin"
                  data-agent-run-id={proposal.source_agent_run_id}
                  data-recovery-epoch-id={proposal.recovery_epoch_id ?? ""}
                >
                  {zh
                    ? "这次调整由旅行 Agent 根据最新事实自主提出；最终执行仍受当前行程权限约束。"
                    : "Your travel Agent proposed this change from the latest facts; execution still follows the Journey's current authority."}
                </p>
              )}
              <p className="live-unchanged" data-testid="live-unchanged">
                {proposal.locked_commitments_preserved
                  ? zh
                    ? "已确认的预订和到达/离开时间保持不变。"
                    : "Confirmed bookings and your arrival/departure stay unchanged."
                  : zh
                    ? "注意：部分已确认内容会受影响。"
                    : "Note: some confirmed items are affected."}
              </p>
              {(removed.length > 0 || added.length > 0 || moved.length > 0) && (
                <dl className="live-diff" data-testid="live-diff">
                  {removed.length > 0 && (
                    <div>
                      <dt>{zh ? "移出" : "Removed"}</dt>
                      <dd data-testid="live-diff-removed">{removed.join("、")}</dd>
                    </div>
                  )}
                  {added.length > 0 && (
                    <div>
                      <dt>{zh ? "换成" : "Replaced with"}</dt>
                      <dd data-testid="live-diff-added">{added.join("、")}</dd>
                    </div>
                  )}
                  {moved.length > 0 && (
                    <div>
                      <dt>{zh ? "调整时间" : "Moved"}</dt>
                      <dd data-testid="live-diff-moved">{moved.join("、")}</dd>
                    </div>
                  )}
                </dl>
              )}
              {journeyCompleted ? (
                <p className="v8-muted" data-testid="live-advisory-only">
                  {t("live.advisoryOnly")}
                </p>
              ) : (
                <div className="live-actions">
                  <button
                    type="button"
                    className="v8-primary-button"
                    data-testid="live-accept"
                    disabled={busy !== null}
                    onClick={() => void accept(proposal.id)}
                  >
                    {busy === proposal.id
                      ? zh ? "应用中…" : "Applying…"
                      : zh ? "接受新方案" : "Accept new plan"}
                  </button>
                  <button
                    type="button"
                    className="v8-outline-button"
                    data-testid="live-today-only"
                    disabled={busy !== null}
                    onClick={() =>
                      setMessage(
                        zh ? "只调整今天：已记录，今天的安排会优先。" : "Adjust today only: noted.",
                      )
                    }
                  >
                    {zh ? "仅调整今天" : "Adjust today only"}
                  </button>
                  <button
                    type="button"
                    className="v8-outline-button"
                    data-testid="live-remind-later"
                    disabled={busy !== null}
                    onClick={defer}
                  >
                    {zh ? "稍后提醒" : "Remind me later"}
                  </button>
                </div>
              )}
            </article>
          );
        })
      ) : (
        /* ORD-0407 ③: this cell deliberately carries no action, and that is a decision, not a leftover.
           It says nothing is waiting for a decision, so any button here would offer work the server did
           not report -- the exact overreach ORD-0384 was narrowed for refusing. The screen already
           carries the one action that could change this reading (`live-update-location`, and
           `live-report-rain` beside it), measured above the 768 px line, so "no action in this cell" is
           not "no way forward on this screen". e2e/qoder-ord0407-empty-state-actions.spec.ts judges
           both halves; if this sentence ever gains a button, that cell goes red on purpose. */
        <p className="v8-muted" data-testid="live-no-proposal">
          {collectiveProposalIds.size > 0
            ? zh
              ? "这些方案属于共享恢复，必须按同行者的共同决定处理。"
              : "These options belong to a shared recovery and can only be handled through the group decision."
            : zh ? "当前没有需要你决定的调整。" : "There is nothing for you to decide right now."}
        </p>
      )}

      {(handled.length > 0 || handledDisruptions.length > 0) && openDisruptions.length === 0 && (
        <p className="live-handled" data-testid="live-handled">
          {zh
            ? "刚才的行程变化已经处理好，已确认的预订保持不变。"
            : "The earlier disruption has been handled; your confirmed bookings are unchanged."}
        </p>
      )}

      {message && (
        <p className="role-alert" role="status" data-testid="live-message">
          {message}
        </p>
      )}
    </section>
  );
}
