import { useCallback, useEffect, useRef, useState } from "react";
import {
  cityNexusApi,
  type GroupRendezvousRouteResult,
  type JourneyMembershipInvitationInboxItem,
  type JourneyMembershipInvitationManagerItem,
  type JourneyOperatorProjection,
  type JourneyRendezvousCoordinationSignal,
  type RendezvousBuddyExecutionProjection,
  type RendezvousBuddyHandoff,
  type RendezvousBuddyHandoffCandidates,
  type RendezvousBuddyPrivateRouteResult,
} from "../../api/citynexus";
import { useLocale } from "../../i18n/LocaleContext";
import { buildGoogleWalkingDirectionsUrl, buildOpenStreetMapPinUrl } from "./memberSideQuestNavigationLinks";

type Props = {
  journeyId: string | null;
  journeyStatus?: string | null;
  recentlyJoinedJourneyId?: string | null;
  onRefreshLocation?: (privateHandoffId?: string, signal?: AbortSignal) => Promise<string | null>;
  onJourneyJoined: (journeyId: string) => Promise<void> | void;
};

type GroupPresence = NonNullable<JourneyOperatorProjection["group_presence"]>;
type Rendezvous = NonNullable<
  GroupPresence["rendezvous"]
>;
type BuddyRouteConsent =
  | { handoffId: string; step: "LOCATION" }
  | { handoffId: string; step: "PROVIDER"; locationObservationId: string };
type BuddyDirectionsConsent = {
  handoff: RendezvousBuddyHandoff;
  handoffSnapshotKey: string;
  targetKey: string;
  audienceKey: string;
};
type BuddyReturnDirectionsConsent = {
  handoffId: string;
  executionId: string;
  targetKey: string;
  sharedStopName: string;
};

function groupPresenceTargetKey(presence: GroupPresence | null | undefined): string | null {
  const target = presence?.rendezvous;
  if (!target) return null;
  return JSON.stringify([
    presence?.active_plan_version_id ?? null,
    presence?.current_activity_id ?? null,
    target.activity_id,
    target.title,
    target.place_id,
    target.place_name,
    target.latitude,
    target.longitude,
    target.coordinate_system,
  ]);
}

function hasExactPublicPin(destination: Rendezvous | null | undefined): boolean {
  return destination?.coordinate_system === "WGS84"
    && typeof destination.latitude === "number"
    && typeof destination.longitude === "number"
    && Number.isFinite(destination.latitude)
    && Number.isFinite(destination.longitude)
    && destination.latitude >= -90 && destination.latitude <= 90
    && destination.longitude >= -180 && destination.longitude <= 180;
}

function rendezvousAudienceKey(members: GroupPresence["members"]): string {
  return JSON.stringify(members.map((member) => member.member_id).sort());
}

function buddyHandoffSnapshotKey(handoff: RendezvousBuddyHandoff): string {
  // Keep the user's consent attached to the exact accepted record, destination, expiry,
  // and original shared-stop reference that was shown in the dialog.
  return JSON.stringify(handoff);
}

function buddyHandoffCanNavigate(handoff: RendezvousBuddyHandoff): boolean {
  if (handoff.status !== "ACCEPTED") return false;
  const execution = handoff.execution;
  if (execution) {
    if (execution.status === "ACTIVE") {
      return execution.context_current === true
        && Boolean(execution.expires_at && Date.parse(execution.expires_at) > Date.now());
    }
    // A completed, cancelled, expired or stale pair execution cannot keep
    // licensing navigation to an obsolete private handoff destination.
    if (execution.status !== "WINDOW_OFFERED") return false;
  }
  // Legacy R79 acceptance and not-yet-accepted execution offers retain
  // the original 20-minute handoff expiry. An ACTIVE R82 lease overrides it.
  return Date.parse(handoff.expires_at) > Date.now();
}

function handoffStillTargetsCurrentSharedStop(
  handoff: RendezvousBuddyHandoff,
  presence: GroupPresence | null,
): boolean {
  const target = presence?.rendezvous;
  return Boolean(
    presence?.active_plan_version_id
    && target
    && target.activity_id === handoff.original_shared_stop.activity_id
    && target.place_id === handoff.original_shared_stop.place_id
    && target.place_name === handoff.original_shared_stop.place_name,
  );
}

function secureJourneyRequestKey(): string {
  // getRandomValues is supported on local-network HTTP pages where
  // randomUUID/subtle may be unavailable. Never fall back to Math.random
  // for an idempotency key that can authorize a signed member write.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return "journey-" + Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function routeEvidenceClass(result: GroupRendezvousRouteResult): "LIVE" | "REPLAY" | "UNKNOWN" {
  const provenance = String(result.route.provenance ?? "").toUpperCase();
  const provider = String(result.route.provider ?? "").trim();
  // A route with a mock/fixture provider is never live, even if its response
  // incorrectly advertises a live provenance token and fresh timestamp.
  const syntheticProvider = /(?:^|[^a-z])(mock|fixture|synthetic|test)(?:$|[^a-z])/i.test(provider);
  const duration = result.route.duration_min;
  const capturedAt = Date.parse(String(result.route.captured_at ?? ""));
  const ageMs = Date.now() - capturedAt;
  if (
    result.status.toUpperCase() !== "COMPLETED"
    || result.plan_unchanged !== true
    || result.target_unchanged !== true
    || !provider
    || syntheticProvider
    || typeof duration !== "number"
    || !Number.isFinite(duration)
    || duration < 0
    || !Number.isFinite(capturedAt)
    || ageMs < -30_000
    || ageMs > 5 * 60_000
  ) return "UNKNOWN";
  if (["LIVE_PROVIDER_CAPTURE", "LIVE_CAPTURE"].includes(provenance)) return "LIVE";
  if (["CAPTURED_REPLAY", "SEALED_REAL_REPLAY"].includes(provenance)) return "REPLAY";
  return "UNKNOWN";
}

function inviteFailureCopy(code: string, zh: boolean): string {
  if (code.includes("RECIPIENT_ACCEPTANCE_REQUIRED")) {
    return zh ? "旅伴必须亲自接受后才会加入；没有写入任何成员。" : "The traveller must accept before joining. No membership was added.";
  }
  if (code.includes("MEMBER_MANAGE")) {
    return zh ? "只有当前有成员管理权限的旅伴可以发出邀请。" : "Only a current Journey member with member-management authority can invite someone.";
  }
  if (code.includes("ALREADY_PENDING")) {
    return zh ? "这位旅伴已有一个待处理邀请。" : "This traveller already has a pending invitation.";
  }
  if (code.includes("ACTIVE_JOURNEY")) {
    return zh ? "行程开始并选定当前计划后，才能邀请旅伴加入。" : "Invite a companion after the Journey is active with a selected plan.";
  }
  return zh ? "邀请没有完成；成员名单没有改变，请刷新后重试。" : "The invitation did not complete. The member set is unchanged; refresh and retry.";
}

function coordinationFailureCopy(code: string, zh: boolean): string {
  if (code.includes("WITHDRAW_TARGET_STALE")) {
    return zh
      ? "你先前看到的状态已被替换、过期或撤回。它不代表当前状态已停止分享；请查看刚刷新后的团队状态。"
      : "The previously displayed status was replaced, expired, or revoked. This does not stop a newer status; check the refreshed team view.";
  }
  if (code.includes("CONTEXT_CHANGED") || code.includes("TARGET_CHANGED")) {
    return zh
      ? "共享计划或会合点刚刚变化。状态没有分享，请刷新后重新确认对象。"
      : "The shared plan or rendezvous changed. Nothing was shared; refresh and confirm the current stop.";
  }
  if (code.includes("GROUP_RECOVERY_FOLLOWTHROUGH_ACTIVE")) {
    return zh
      ? "团队正在完成已批准的恢复会合；此状态暂不可替代恢复确认。"
      : "The group is completing an approved recovery rendezvous. This self-report cannot replace its confirmation.";
  }
  if (code.includes("PLANNED_SPLIT_TARGET_UNSUPPORTED")) {
    return zh
      ? "你当前有独立的计划支线；请使用该支线的会合安排。"
      : "You are on a planned individual side trip. Use that trip's rendezvous flow.";
  }
  return zh
    ? "状态没有更新。请刷新团队状态后重试；当前共享计划没有改变。"
    : "The status was not updated. Refresh the team status and retry; the shared plan is unchanged.";
}

function coordinationStatusLabel(signal: JourneyRendezvousCoordinationSignal, zh: boolean): string {
  return signal.signal_kind === "HEADING_TO_RENDEZVOUS"
    ? (zh ? `${signal.display_name} 自报正在前往会合点` : `${signal.display_name} self-reports heading to the rendezvous`)
    : (zh ? `${signal.display_name} 自报需要协调帮助` : `${signal.display_name} self-reports needing coordination help`);
}

export function JourneyMembershipPanel({
  journeyId,
  journeyStatus,
  recentlyJoinedJourneyId = null,
  onRefreshLocation,
  onJourneyJoined,
}: Props) {
  const { locale } = useLocale();
  const zh = locale.startsWith("zh");
  const active = String(journeyStatus ?? "").toUpperCase() === "ACTIVE";
  const [inbox, setInbox] = useState<JourneyMembershipInvitationInboxItem[]>([]);
  const [sent, setSent] = useState<JourneyMembershipInvitationManagerItem[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [targetTravelerId, setTargetTravelerId] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [mapConsent, setMapConsent] = useState(false);
  const [mapConsentKey, setMapConsentKey] = useState<string | null>(null);
  const [routeConsent, setRouteConsent] = useState(false);
  const [routeConsentKey, setRouteConsentKey] = useState<string | null>(null);
  const [navigationBusy, setNavigationBusy] = useState(false);
  const [groupPresence, setGroupPresence] = useState<GroupPresence | null>(null);
  const [coordinationConsentKind, setCoordinationConsentKind] = useState<JourneyRendezvousCoordinationSignal["signal_kind"] | null>(null);
  const [coordinationConsentKey, setCoordinationConsentKey] = useState<string | null>(null);
  const [coordinationConsentAudienceKey, setCoordinationConsentAudienceKey] = useState<string | null>(null);
  const [coordinationBusy, setCoordinationBusy] = useState(false);
  const [coordinationMessage, setCoordinationMessage] = useState<string | null>(null);
  const [buddyHandoffs, setBuddyHandoffs] = useState<RendezvousBuddyHandoff[]>([]);
  const [buddyCandidates, setBuddyCandidates] = useState<RendezvousBuddyHandoffCandidates | null>(null);
  const [buddySelectedPlaceId, setBuddySelectedPlaceId] = useState("");
  const [buddyExecutionWindowMinutes, setBuddyExecutionWindowMinutes] = useState<30 | 60 | 90>(60);
  const [buddyActionBusy, setBuddyActionBusy] = useState(false);
  const [buddyMessage, setBuddyMessage] = useState<string | null>(null);
  const [buddyMapConsentId, setBuddyMapConsentId] = useState<string | null>(null);
  const [buddyDirectionsConsent, setBuddyDirectionsConsent] = useState<BuddyDirectionsConsent | null>(null);
  const [buddyReturnDirectionsConsent, setBuddyReturnDirectionsConsent] = useState<BuddyReturnDirectionsConsent | null>(null);
  const [buddyPrivateRoutes, setBuddyPrivateRoutes] = useState<Record<string, RendezvousBuddyPrivateRouteResult>>({});
  const [buddyRouteConsent, setBuddyRouteConsent] = useState<BuddyRouteConsent | null>(null);
  const [buddyRouteBusy, setBuddyRouteBusy] = useState(false);
  const [buddyRouteMessage, setBuddyRouteMessage] = useState<string | null>(null);
  const buddyRouteLocationAbort = useRef<AbortController | null>(null);
  const [routeResult, setRouteResult] = useState<{
    key: string;
    result: GroupRendezvousRouteResult;
  } | null>(null);
  const rendezvous = groupPresence?.rendezvous ?? null;
  const currentTargetKey = groupPresenceTargetKey(groupPresence);
  const currentAudienceKey = groupPresence ? rendezvousAudienceKey(groupPresence.members) : null;

  const refresh = useCallback(async () => {
    const mine = await cityNexusApi.getMyJourneyMembershipInvitations().catch(() => []);
    setInbox(mine);
    if (!journeyId) {
      setSent([]);
      setCanManage(false);
      return;
    }
    try {
      setSent(await cityNexusApi.getJourneyMembershipInvitations(journeyId));
      setCanManage(true);
    } catch {
      setSent([]);
      setCanManage(false);
    }
  }, [journeyId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const refreshGroupPresence = useCallback(async (): Promise<GroupPresence | null> => {
    if (!journeyId || !active) {
      setGroupPresence(null);
      setBuddyHandoffs([]);
      setBuddyPrivateRoutes({});
      return null;
    }
    try {
      const [projection, handoffs] = await Promise.all([
        cityNexusApi.getJourneyOperator(journeyId),
        cityNexusApi.getCurrentRendezvousBuddyHandoffs(journeyId).catch(() => ({ items: [] })),
      ]);
      const next = projection.group_presence ?? null;
      setGroupPresence(next);
      setBuddyHandoffs(handoffs.items);
      const privateRouteEntries = await Promise.all(
        handoffs.items
          .filter((handoff) => handoff.status === "ACCEPTED")
          .map(async (handoff): Promise<[string, RendezvousBuddyPrivateRouteResult] | null> => {
            try {
              return [handoff.handoff_id, await cityNexusApi.getCurrentRendezvousBuddyPrivateRoute(journeyId, handoff.handoff_id)];
            } catch {
              return null;
            }
          }),
      );
      setBuddyPrivateRoutes(Object.fromEntries(privateRouteEntries.filter(
        (entry): entry is [string, RendezvousBuddyPrivateRouteResult] => entry !== null,
      )));
      return next;
    } catch {
      setGroupPresence(null);
      setBuddyHandoffs([]);
      setBuddyPrivateRoutes({});
      return null;
    }
  }, [active, journeyId]);

  useEffect(() => {
    // A companion still needs the shared meeting point after a reload, not
    // only during the React session in which they pressed Accept.
    if (!journeyId || !active) {
      setGroupPresence(null);
      setBuddyHandoffs([]);
      setBuddyPrivateRoutes({});
      setBuddyRouteConsent(null);
      setRouteResult(null);
      return;
    }
    void refreshGroupPresence();
  }, [journeyId, active, refreshGroupPresence]);

  useEffect(() => {
    setRouteResult((current) => current?.key === currentTargetKey ? current : null);
  }, [currentTargetKey]);

  useEffect(() => {
    // A refreshed member list changes who would see this disclosure. A user
    // who reviewed A/B/C must never silently end up sharing with a new D.
    if (!coordinationConsentKind) return;
    if (
      coordinationConsentKey === currentTargetKey
      && coordinationConsentAudienceKey === currentAudienceKey
    ) return;
    setCoordinationConsentKind(null);
    setCoordinationConsentKey(null);
    setCoordinationConsentAudienceKey(null);
    setCoordinationMessage(zh
      ? "会合点或同行者名单刚刚变化。旧分享确认已关闭；请重新查看受众并主动确认。"
      : "The stop or companion list changed. The previous consent was closed; review the new audience before sharing.");
  }, [
    currentTargetKey, currentAudienceKey, coordinationConsentKind,
    coordinationConsentKey, coordinationConsentAudienceKey, zh,
  ]);

  const coordinationSignals = groupPresence?.coordination_signals ?? [];
  const ownCoordinationSignal = coordinationSignals.find((signal) => signal.viewer_can_withdraw) ?? null;

  const openCoordinationConsent = (kind: JourneyRendezvousCoordinationSignal["signal_kind"]) => {
    if (!currentTargetKey || !currentAudienceKey || !groupPresence?.can_guide) return;
    setCoordinationMessage(null);
    setCoordinationConsentKey(currentTargetKey);
    setCoordinationConsentAudienceKey(currentAudienceKey);
    setCoordinationConsentKind(kind);
  };

  const shareCoordinationSignal = async () => {
    if (coordinationBusy || !journeyId || !coordinationConsentKind) return;
    const current = groupPresence;
    const target = current?.rendezvous;
    const activePlanId = current?.active_plan_version_id;
    if (
      !current || !target || !activePlanId
      || coordinationConsentKey !== currentTargetKey
      || coordinationConsentAudienceKey !== currentAudienceKey
      || !/^[a-f0-9]{64}$/.test(String(current.coordination_audience_sha256 ?? ""))
    ) {
      setCoordinationConsentKind(null);
      setCoordinationConsentKey(null);
      setCoordinationConsentAudienceKey(null);
      setCoordinationMessage(zh
        ? "会合点或接收状态的成员名单已变化；没有扩大分享范围。请刷新并重新确认受众。"
        : "The stop or audience changed. Nothing was shared with new members; refresh and confirm the updated audience.");
      await refreshGroupPresence();
      return;
    }
    setCoordinationBusy(true);
    setCoordinationMessage(null);
    try {
      const receipt = await cityNexusApi.shareRendezvousMemberSignal(
        journeyId,
        {
          signal_kind: coordinationConsentKind,
          expected_active_plan_version_id: activePlanId,
          expected_current_activity_id: current.current_activity_id ?? null,
          expected_rendezvous_activity_id: target.activity_id,
          expected_public_place_id: target.place_id,
          // Digest is derived from the signed in-force cohort on the server;
          // this action works even when crypto.subtle is unavailable on HTTP LAN.
          expected_audience_sha256: String(current.coordination_audience_sha256),
        },
        secureJourneyRequestKey(),
      );
      setCoordinationConsentKind(null);
      setCoordinationConsentKey(null);
      setCoordinationConsentAudienceKey(null);
      setCoordinationMessage(receipt.status === "ACTIVE"
        ? (zh
          ? "你的自愿状态已分享给当前同行者；它会在 45 分钟后过期，不证明已到场。"
          : "Your voluntary status is shared with current companions for up to 45 minutes; it does not prove arrival.")
        : (zh ? "状态已处理。" : "The status was processed."));
      await refreshGroupPresence();
    } catch (error) {
      const text = error instanceof Error ? error.message : "";
      setCoordinationMessage(coordinationFailureCopy(text, zh));
      setCoordinationConsentKind(null);
      setCoordinationConsentKey(null);
      setCoordinationConsentAudienceKey(null);
      await refreshGroupPresence();
    } finally {
      setCoordinationBusy(false);
    }
  };

  const withdrawCoordinationSignal = async () => {
    if (coordinationBusy || !journeyId || !ownCoordinationSignal) return;
    setCoordinationBusy(true);
    setCoordinationMessage(null);
    try {
      const receipt = await cityNexusApi.withdrawRendezvousMemberSignal(journeyId, ownCoordinationSignal.signal_id);
      await refreshGroupPresence();
      setCoordinationMessage(receipt.status === "REVOKED"
        ? (zh ? "已停止向后续查看者分享这条状态。" : "This exact status has been withdrawn from future projections.")
        : (zh
          ? "这条状态已经变化，不保证其他较新的状态已停止分享；请核对刚刷新后的团队状态。"
          : "This status changed; a newer one may still be shared. Check the refreshed team view."));
    } catch (error) {
      await refreshGroupPresence();
      setCoordinationMessage(coordinationFailureCopy(error instanceof Error ? error.message : "", zh));
    } finally {
      setCoordinationBusy(false);
    }
  };

  const acknowledgeCoordinationHelp = async (signal: JourneyRendezvousCoordinationSignal) => {
    if (coordinationBusy || !journeyId || !signal.viewer_can_acknowledge) return;
    setCoordinationBusy(true);
    setCoordinationMessage(null);
    try {
      await cityNexusApi.acknowledgeRendezvousHelpRequest(journeyId, signal.signal_id);
      setCoordinationMessage(zh
        ? "已用你的签名身份回应这次求助；这不表示你在现场等待。"
        : "You acknowledged this help request with your signed identity; this does not mean you are physically waiting there.");
      await refreshGroupPresence();
    } catch (error) {
      setCoordinationMessage(coordinationFailureCopy(error instanceof Error ? error.message : "", zh));
      await refreshGroupPresence();
    } finally {
      setCoordinationBusy(false);
    }
  };

  const loadBuddyHandoffCandidates = async (signal: JourneyRendezvousCoordinationSignal) => {
    if (!journeyId || buddyActionBusy || signal.signal_kind !== "NEEDS_COORDINATION"
      || !signal.viewer_is_acknowledger || !signal.acknowledgement) return;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      const candidates = await cityNexusApi.getRendezvousBuddyHandoffCandidates(journeyId, signal.signal_id);
      setBuddyCandidates(candidates);
      setBuddySelectedPlaceId(candidates.items[0]?.place_id ?? "");
    } catch {
      setBuddyMessage(zh
        ? "当前求助或同行者名单已变化；没有创建接应提议。请刷新团队状态。"
        : "The help request or companion set changed. No handoff was offered; refresh the team status.");
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const offerBuddyHandoff = async () => {
    const candidates = buddyCandidates;
    const current = groupPresence;
    const original = current?.rendezvous;
    if (!journeyId || !candidates || !current || !original || !buddySelectedPlaceId || buddyActionBusy) return;
    if (
      current.active_plan_version_id !== candidates.active_plan_version_id
      || (current.current_activity_id ?? null) !== candidates.current_activity_id
      || original.activity_id !== candidates.rendezvous_activity_id
      || original.place_id !== candidates.original_public_place_id
      || current.coordination_audience_sha256 !== candidates.audience_sha256
    ) {
      setBuddyCandidates(null);
      setBuddyMessage(zh
        ? "共享计划、会合点或同行者名单已变化；旧地点没有发送。请重新加载建议。"
        : "The plan, stop, or companion set changed. The old place was not offered; reload suggestions.");
      return;
    }
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      await cityNexusApi.offerRendezvousBuddyHandoff(journeyId, {
        help_signal_id: candidates.help_signal_id,
        expected_active_plan_version_id: candidates.active_plan_version_id,
        expected_current_activity_id: candidates.current_activity_id,
        expected_rendezvous_activity_id: candidates.rendezvous_activity_id,
        expected_original_public_place_id: candidates.original_public_place_id,
        expected_audience_sha256: candidates.audience_sha256,
        handoff_place_id: buddySelectedPlaceId,
        execution_window_minutes: buddyExecutionWindowMinutes,
      }, secureJourneyRequestKey());
      setBuddyCandidates(null);
      setBuddySelectedPlaceId("");
      setBuddyMessage(zh
        ? `已将公开地点和 ${buddyExecutionWindowMinutes} 分钟接应窗口一起提议给求助者；只有对方本人接受后窗口才开始，共享行程未改变。`
        : `The public handoff place and a ${buddyExecutionWindowMinutes}-minute window were offered privately. The window starts only if the requester accepts; the shared Journey is unchanged.`);
      await refreshGroupPresence();
    } catch {
      setBuddyMessage(zh
        ? "接应提议没有创建；请刷新核对求助、地点和共享计划后重试。"
        : "The handoff was not created. Refresh and recheck the request, place, and shared plan.");
      await refreshGroupPresence();
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const respondToBuddyHandoff = async (handoff: RendezvousBuddyHandoff, decision: "ACCEPT" | "DECLINE") => {
    if (!journeyId || buddyActionBusy || handoff.viewer_role !== "REQUESTER") return;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      await cityNexusApi.respondToRendezvousBuddyHandoff(journeyId, handoff.handoff_id, decision);
      setBuddyMessage(decision === "ACCEPT"
        ? (handoff.execution?.status === "WINDOW_OFFERED"
          ? (zh
            ? `你已签名接受公开接应地点和 ${handoff.execution.execution_window_minutes} 分钟执行窗口；窗口从现在开始。`
            : `You signed to accept the public handoff and its ${handoff.execution.execution_window_minutes}-minute execution window. The window starts now.`)
          : (zh ? "已接受这处仅供双方协调的公共会合意向；原共同计划未改变。" : "You accepted this private public-place handoff intention. The original shared plan is unchanged."))
        : (zh ? "已拒绝这次提议；原共同计划未改变。" : "You declined this offer. The original shared plan is unchanged."));
      await refreshGroupPresence();
    } catch {
      setBuddyMessage(zh
        ? "提议已过期或上下文变化；没有接受旧地点。请刷新。"
        : "The offer expired or its context changed. The stale place was not accepted; refresh.");
      await refreshGroupPresence();
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const offerBuddyExecutionWindow = async (
    handoff: RendezvousBuddyHandoff,
    windowMinutes: 30 | 60 | 90,
  ) => {
    if (!journeyId || buddyActionBusy || handoff.viewer_role !== "HELPER" || handoff.status !== "ACCEPTED") return;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      await cityNexusApi.offerRendezvousBuddyExecutionWindow(
        journeyId, handoff.handoff_id, windowMinutes, secureJourneyRequestKey(),
      );
      setBuddyMessage(zh
        ? `已单独提出 ${windowMinutes} 分钟接应执行窗口；对方签名接受前不会开始。`
        : `You offered a separate ${windowMinutes}-minute execution window. It does not start until the requester signs to accept.`);
      await refreshGroupPresence();
    } catch {
      setBuddyMessage(zh
        ? "执行窗口没有创建；原提议或共享计划可能已经过期，请刷新后重新确认。"
        : "The execution window was not created. The handoff or shared context may have expired; refresh and review it.");
      await refreshGroupPresence();
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const acceptBuddyExecutionWindow = async (handoff: RendezvousBuddyHandoff) => {
    if (!journeyId || buddyActionBusy || handoff.viewer_role !== "REQUESTER") return;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      await cityNexusApi.acceptRendezvousBuddyExecutionWindow(journeyId, handoff.handoff_id);
      setBuddyMessage(zh ? "你已签名接受有限接应窗口；现在才开始计时。" : "You signed to accept the bounded assist window. Its clock starts now.");
      await refreshGroupPresence();
    } catch {
      setBuddyMessage(zh
        ? "窗口提议已经过期或上下文变化；没有延长旧接应。请刷新并重新协商。"
        : "The window offer expired or its context changed. The old handoff was not extended; refresh and agree again.");
      await refreshGroupPresence();
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const reportBuddyExecution = async (
    handoff: RendezvousBuddyHandoff,
    execution: RendezvousBuddyExecutionProjection,
    receiptKind: "BUDDY_MET_ACK" | "CANNOT_MEET" | "CANCEL_PARTICIPATION",
  ) => {
    if (!journeyId || buddyActionBusy || !execution.can_report) return;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      const receipt = await cityNexusApi.reportRendezvousBuddyExecution(
        journeyId, handoff.handoff_id, execution.execution_id, receiptKind, secureJourneyRequestKey(),
      );
      setBuddyMessage(receipt.status === "MUTUALLY_ACKNOWLEDGED"
        ? (zh
          ? "双方已分别签名确认接应协作完成。应用记录的是双方自报，不是设备验证的物理到场。"
          : "Both members independently confirmed the assist. This records their self-reports, not device-verified physical presence.")
        : receipt.status === "REPLAN_REQUIRED"
          ? (zh ? "已记录你无法在此接应；不会把旧意向当作完成，可重新协调或返回当前共同会合点。" : "Your inability to meet was recorded. The old intention is not marked complete; you can replan or return to the current shared stop.")
          : receipt.status === "CANCELLED"
            ? (zh ? "已记录你退出这次接应；对方的状态不会替你确认，仍可单独导航回当前共同会合点。" : "Your participation was cancelled. The other member cannot confirm for you; you may navigate back to the current shared stop.")
            : (zh ? "已记录你的本人自报；仍需对方独立确认。" : "Your personal self-report was recorded; the other member must confirm independently."));
      await refreshGroupPresence();
    } catch {
      setBuddyMessage(zh
        ? "自报没有写入；接应窗口可能已过期或计划已变化。刷新核对当前状态后再决定。"
        : "The self-report was not recorded. The window may have expired or the plan changed; refresh before deciding again.");
      await refreshGroupPresence();
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const closeBuddyHandoff = async (handoff: RendezvousBuddyHandoff) => {
    if (!journeyId || buddyActionBusy) return;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      if (handoff.viewer_role === "HELPER") {
        await cityNexusApi.cancelRendezvousBuddyHandoff(journeyId, handoff.handoff_id);
      } else {
        await cityNexusApi.revokeRendezvousBuddyHandoff(journeyId, handoff.handoff_id);
      }
      setBuddyMessage(zh ? "双方接应提议已关闭；共同计划没有改变。" : "The pair handoff is closed. The shared plan is unchanged.");
      if (buddyMapConsentId === handoff.handoff_id) setBuddyMapConsentId(null);
      if (buddyDirectionsConsent?.handoff.handoff_id === handoff.handoff_id) setBuddyDirectionsConsent(null);
      await refreshGroupPresence();
    } catch {
      setBuddyMessage(zh ? "提议已过期或已关闭；请刷新查看当前状态。" : "The handoff expired or was already closed. Refresh to see the current state.");
      await refreshGroupPresence();
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const openAcceptedBuddyMap = async () => {
    const handoffId = buddyMapConsentId;
    if (!journeyId || !handoffId || buddyActionBusy) return;
    const popup = window.open("about:blank", "_blank");
    if (!popup) {
      setBuddyMessage(zh ? "浏览器阻止了新窗口；允许弹窗后重新确认。" : "The browser blocked the new tab. Allow popups and confirm again.");
      return;
    }
    popup.opener = null;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      const fresh = await cityNexusApi.getCurrentRendezvousBuddyHandoffs(journeyId);
      const handoff = fresh.items.find((item) => item.handoff_id === handoffId && item.status === "ACCEPTED");
      const place = handoff?.handoff_place;
      const stillValid = handoff && buddyHandoffCanNavigate(handoff)
        && place?.coordinate_system === "WGS84"
        && Number.isFinite(place.latitude) && place.latitude >= -90 && place.latitude <= 90
        && Number.isFinite(place.longitude) && place.longitude >= -180 && place.longitude <= 180;
      if (!stillValid || !place) {
        popup.close();
        setBuddyMessage(zh
          ? "提议已撤回、过期或变更；没有向 OpenStreetMap 发送地点。"
          : "The handoff is no longer accepted, current, or valid. No place was sent to OpenStreetMap.");
        setBuddyMapConsentId(null);
        await refreshGroupPresence();
        return;
      }
      popup.location.href = buildOpenStreetMapPinUrl({
        latitude: place.latitude,
        longitude: place.longitude,
        coordinate_system: "WGS84",
      });
      setBuddyMapConsentId(null);
    } catch {
      popup.close();
      setBuddyMessage(zh
        ? "无法重新验证当前双方同意；没有向地图发送地点。"
        : "The mutual handoff could not be revalidated. No place was sent to the map.");
      setBuddyMapConsentId(null);
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const beginBuddyDirectionsConsent = (handoff: RendezvousBuddyHandoff) => {
    if (!buddyHandoffCanNavigate(handoff) || !currentTargetKey || !currentAudienceKey
      || !handoffStillTargetsCurrentSharedStop(handoff, groupPresence)) {
      setBuddyMessage(zh
        ? "当前共享计划或接应目标尚未核对；请先刷新双方接应状态。"
        : "The current shared plan and handoff target are not verified yet. Refresh the pair handoff first.");
      return;
    }
    setBuddyMessage(null);
    setBuddyDirectionsConsent({
      handoff,
      handoffSnapshotKey: buddyHandoffSnapshotKey(handoff),
      targetKey: currentTargetKey,
      audienceKey: currentAudienceKey,
    });
  };

  const beginBuddyReturnDirectionsConsent = (handoff: RendezvousBuddyHandoff) => {
    const stop = groupPresence?.rendezvous;
    const execution = handoff.execution;
    if (
      handoff.status !== "ACCEPTED"
      || !execution?.can_return_to_shared_stop
      || !execution.context_current
      || !currentTargetKey
      || !stop
      || !hasExactPublicPin(stop)
      || !handoffStillTargetsCurrentSharedStop(handoff, groupPresence)
    ) {
      setBuddyMessage(zh
        ? "接应结果或当前共同会合点尚未重新核对；没有打开地图，请刷新后重新确认。"
        : "The pair outcome or current shared stop is not verified. No map was opened; refresh and confirm again.");
      return;
    }
    setBuddyMessage(null);
    setBuddyReturnDirectionsConsent({
      handoffId: handoff.handoff_id,
      executionId: execution.execution_id,
      targetKey: currentTargetKey,
      sharedStopName: stop.place_name,
    });
  };

  const openBuddyReturnDirections = async () => {
    const consent = buddyReturnDirectionsConsent;
    if (!journeyId || !consent || buddyActionBusy) return;
    const popup = window.open("about:blank", "_blank");
    if (!popup) {
      setBuddyMessage(zh
        ? "浏览器阻止了新窗口；没有向地图发送地点。请允许弹窗后重新确认。"
        : "The browser blocked the new tab; no destination was sent. Allow popups and confirm again.");
      return;
    }
    popup.opener = null;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      // This is a fresh, destination-only consent. Recheck both the signed pair
      // outcome and current shared plan/cursor immediately before opening Maps.
      const [projection, current] = await Promise.all([
        cityNexusApi.getJourneyOperator(journeyId),
        cityNexusApi.getCurrentRendezvousBuddyHandoffs(journeyId),
      ]);
      const presence = projection.group_presence ?? null;
      const freshHandoff = current.items.find((item) => item.handoff_id === consent.handoffId);
      const execution = freshHandoff?.execution;
      const stop = presence?.rendezvous;
      const freshTargetKey = groupPresenceTargetKey(presence);
      const stillCurrent = freshHandoff?.status === "ACCEPTED"
        && execution?.execution_id === consent.executionId
        && execution.can_return_to_shared_stop
        && execution.context_current
        && freshTargetKey === consent.targetKey
        && Boolean(stop && presence?.active_plan_version_id && hasExactPublicPin(stop));

      setGroupPresence(presence);
      setBuddyHandoffs(current.items);
      if (!stillCurrent || !stop) {
        popup.close();
        setBuddyReturnDirectionsConsent(null);
        setBuddyMessage(zh
          ? "接应结果、当前共同会合点或计划游标已变化；没有向外部地图发送地点。请刷新后重新同意。"
          : "The pair outcome, current shared stop, or plan cursor changed. No destination was sent; refresh and consent again.");
        return;
      }

      popup.location.href = buildGoogleWalkingDirectionsUrl({
        kind: "SHARED_RENDEZVOUS",
        place_id: stop.place_id,
        name: stop.place_name,
        latitude: stop.latitude,
        longitude: stop.longitude,
        coordinate_system: stop.coordinate_system,
        provenance: {
          provider_key: "citynexus-current-shared-plan-stop",
          external_id: stop.activity_id,
          source_coordinate_system: stop.coordinate_system,
        },
        navigation_available: true,
        unavailable_reason: null,
      });
      setBuddyReturnDirectionsConsent(null);
      setBuddyMessage(zh
        ? "已将当前共同会合点作为唯一目的地交给 Google Maps；链接不含 CityNexus 起点或路线，也不代表已重新加入或实际到场。"
        : "The current shared stop was sent as the only destination to Google Maps. The link contains no CityNexus origin or route and does not prove rejoining or physical arrival.");
    } catch {
      popup.close();
      setBuddyReturnDirectionsConsent(null);
      setBuddyMessage(zh
        ? "无法重新核对接应结果和当前共同会合点；没有向外部地图发送地点。"
        : "The pair outcome and current shared stop could not be revalidated. No destination was sent to an external map.");
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const openAcceptedBuddyDirections = async () => {
    const consent = buddyDirectionsConsent;
    if (!journeyId || !consent || buddyActionBusy) return;
    const popup = window.open("about:blank", "_blank");
    if (!popup) {
      setBuddyMessage(zh
        ? "浏览器阻止了新窗口；没有向地图发送地点。请允许弹窗后重新确认。"
        : "The browser blocked the new tab; no destination was sent. Allow popups and confirm again.");
      return;
    }
    popup.opener = null;
    setBuddyActionBusy(true);
    setBuddyMessage(null);
    try {
      // Re-read both caller-private handoff state and the live shared plan/cursor
      // immediately before any external destination is released.
      const [projection, current] = await Promise.all([
        cityNexusApi.getJourneyOperator(journeyId),
        cityNexusApi.getCurrentRendezvousBuddyHandoffs(journeyId),
      ]);
      const presence = projection.group_presence ?? null;
      const freshHandoff = current.items.find((item) => item.handoff_id === consent.handoff.handoff_id);
      const freshTargetKey = groupPresenceTargetKey(presence);
      const freshAudienceKey = presence ? rendezvousAudienceKey(presence.members) : null;
      const place = freshHandoff?.handoff_place;
      const exactPublicDestination = place
        && (place.source_kind === "CURRENT_SHARED_PLAN_STOP" || place.source_kind === "PUBLIC_PLACE_CATALOG_RECORD")
        && place.coordinate_system === "WGS84"
        && Number.isFinite(place.latitude) && place.latitude >= -90 && place.latitude <= 90
        && Number.isFinite(place.longitude) && place.longitude >= -180 && place.longitude <= 180;
      const stillCurrent = Boolean(freshHandoff && buddyHandoffCanNavigate(freshHandoff))
        && freshHandoff !== undefined
        && buddyHandoffSnapshotKey(freshHandoff) === consent.handoffSnapshotKey
        && freshTargetKey === consent.targetKey
        && freshAudienceKey === consent.audienceKey
        && handoffStillTargetsCurrentSharedStop(freshHandoff, presence)
        && exactPublicDestination;

      setGroupPresence(presence);
      setBuddyHandoffs(current.items);
      if (!stillCurrent || !freshHandoff || !place) {
        popup.close();
        setBuddyDirectionsConsent(null);
        setBuddyMessage(zh
          ? "接应同意、成员、地点或共同计划游标已变化或过期；没有向外部地图发送地点。请刷新后重新同意。"
          : "The handoff, members, destination, or shared-plan cursor changed or expired. No destination was sent to an external map; refresh and consent again.");
        return;
      }

      const url = buildGoogleWalkingDirectionsUrl({
        kind: "SHARED_RENDEZVOUS",
        place_id: place.place_id,
        name: place.name,
        latitude: place.latitude,
        longitude: place.longitude,
        coordinate_system: place.coordinate_system,
        provenance: {
          provider_key: place.source_provider_key,
          external_id: place.source_external_id,
          source_coordinate_system: place.coordinate_system,
        },
        navigation_available: true,
        unavailable_reason: null,
      });
      popup.location.href = url;
      setBuddyDirectionsConsent(null);
      setBuddyMessage(zh
        ? "已把双方当前同意的目的地交给 Google Maps；路线和 ETA 由外部应用自行处理，不代表服务器验证路线或实际到场。"
        : "The currently agreed destination was handed to Google Maps. The external app handles directions and any ETA; this is not a server-verified route or proof of arrival.");
    } catch {
      popup.close();
      setBuddyDirectionsConsent(null);
      setBuddyMessage(zh
        ? "无法重新验证双方接应和当前共同计划；没有向外部地图发送地点。"
        : "The pair handoff and current shared plan could not be revalidated. No destination was sent to an external map.");
    } finally {
      setBuddyActionBusy(false);
    }
  };

  const beginBuddyRouteCheck = (handoff: RendezvousBuddyHandoff) => {
    if (!onRefreshLocation || buddyRouteBusy || buddyActionBusy) return;
    setBuddyRouteMessage(null);
    setBuddyRouteConsent({ handoffId: handoff.handoff_id, step: "LOCATION" });
  };

  const confirmBuddyRouteLocation = async () => {
    if (!journeyId || !onRefreshLocation || !buddyRouteConsent || buddyRouteConsent.step !== "LOCATION" || buddyRouteBusy) return;
    const controller = new AbortController();
    buddyRouteLocationAbort.current = controller;
    setBuddyRouteBusy(true);
    setBuddyRouteMessage(null);
    try {
      const observationId = await onRefreshLocation(buddyRouteConsent.handoffId, controller.signal);
      if (controller.signal.aborted) return;
      if (!observationId) {
        setBuddyRouteMessage(zh
          ? "没有保存位置观察；没有发出接应路线请求。"
          : "No location observation was saved; no handoff route was requested.");
        setBuddyRouteConsent(null);
        return;
      }
      setBuddyRouteConsent({
        handoffId: buddyRouteConsent.handoffId,
        step: "PROVIDER",
        locationObservationId: observationId,
      });
    } catch {
      setBuddyRouteMessage(zh
        ? "前台位置读取失败；没有发出路线请求。"
        : "Foreground location capture failed; no route was requested.");
      setBuddyRouteConsent(null);
    } finally {
      if (buddyRouteLocationAbort.current === controller) buddyRouteLocationAbort.current = null;
      setBuddyRouteBusy(false);
    }
  };

  const confirmBuddyRouteProviderDisclosure = async () => {
    if (!journeyId || !buddyRouteConsent || buddyRouteConsent.step !== "PROVIDER" || buddyRouteBusy) return;
    const { handoffId, locationObservationId } = buddyRouteConsent;
    setBuddyRouteBusy(true);
    setBuddyRouteMessage(null);
    try {
      const result = await cityNexusApi.acquireRendezvousBuddyPrivateRoute(journeyId, handoffId, {
        location_observation_id: locationObservationId,
        requested_mode: "WALK",
        external_provider_disclosure_confirmed: true,
      });
      setBuddyPrivateRoutes((current) => ({ ...current, [handoffId]: result }));
      setBuddyRouteConsent(null);
      setBuddyRouteMessage(result.route_state === "UNKNOWN"
        ? (zh ? "路线保持 UNKNOWN；没有用模拟分钟数填补。" : "Route remains UNKNOWN; no mock duration was substituted.")
        : (zh ? "已取得仅你可见的路线结果；共同计划未改变。" : "Your private route result is ready; the shared Journey is unchanged."));
      await refreshGroupPresence();
    } catch {
      setBuddyRouteConsent(null);
      setBuddyRouteMessage(zh
        ? "接应、成员或计划上下文已变化；路线没有显示，请刷新后重新同意。"
        : "The handoff, member, or plan context changed. No route was shown; refresh and consent again.");
      await refreshGroupPresence();
    } finally {
      setBuddyRouteBusy(false);
    }
  };

  const cancelBuddyRouteConsent = () => {
    if (buddyRouteBusy && buddyRouteConsent?.step !== "LOCATION") return;
    if (buddyRouteConsent?.step === "LOCATION" && buddyRouteBusy) {
      buddyRouteLocationAbort.current?.abort();
      setBuddyRouteBusy(false);
      setBuddyRouteConsent(null);
      setBuddyRouteMessage(zh
        ? "已取消前台位置读取；不会继续发出路线请求。若位置保存请求已经提交，其结果可能仍在服务端处理中。"
        : "Foreground location capture was cancelled; no route request will follow. If a location-save request was already submitted, the server may still be processing it.");
      return;
    }
    const observationWasSaved = buddyRouteConsent?.step === "PROVIDER";
    setBuddyRouteConsent(null);
    if (observationWasSaved) {
      setBuddyRouteMessage(zh
        ? "位置观察已留在 Journey 中；你取消了外部路线请求。"
        : "The location observation remains in the Journey; you cancelled the external route request.");
    }
  };

  const createInvitation = async () => {
    const target = targetTravelerId.trim();
    if (!target || busyId) return;
    setBusyId("create");
    setMessage(null);
    try {
      const created = await cityNexusApi.createJourneyMembershipInvitation(
        journeyId,
        target,
        secureJourneyRequestKey(),
      );
      setTargetTravelerId("");
      setMessage(zh
        ? `已邀请这位旅伴作为 COLLABORATOR。TA 接受前不会出现在成员名单中。有效期至 ${new Date(created.expires_at).toLocaleString(locale)}。`
        : `Collaborator invitation sent. They will not enter the member set until they accept. Expires ${new Date(created.expires_at).toLocaleString(locale)}.`);
      await refresh();
    } catch (error) {
      const text = error instanceof Error ? error.message : "";
      setMessage(inviteFailureCopy(text, zh));
    } finally {
      setBusyId(null);
    }
  };

  const respond = async (
    invitation: JourneyMembershipInvitationInboxItem,
    decision: "ACCEPT" | "DECLINE",
  ) => {
    if (busyId) return;
    setBusyId(invitation.invitation_id);
    setMessage(null);
    try {
      const result = await cityNexusApi.respondToJourneyMembershipInvitation(
        invitation.invitation_id,
        decision,
      );
      await refresh();
      if (decision === "ACCEPT" && result.journey_id) {
        try {
          await onJourneyJoined(result.journey_id);
        } catch {
          setMessage(zh
            ? "你已加入这趟行程，但当前页面未能加载；刷新页面即可继续。"
            : "You joined the Journey, but this page could not load it. Refresh to continue.");
          return;
        }
        setMessage(zh
          ? "你已作为 COLLABORATOR 加入这趟行程。现有地点分享或待决策可能因成员名单变化而需要重新确认。"
          : "You joined as a collaborator. Existing Place shares and pending decisions may need renewed consent because the member set changed.");
      } else {
        setMessage(zh ? "已谢绝邀请；成员名单没有改变。" : "Invitation declined. The member set is unchanged.");
      }
    } catch (error) {
      setMessage(inviteFailureCopy(error instanceof Error ? error.message : "", zh));
      await refresh();
    } finally {
      setBusyId(null);
    }
  };

  const revoke = async (invitationId: string) => {
    if (busyId) return;
    setBusyId(invitationId);
    setMessage(null);
    try {
      await cityNexusApi.revokeJourneyMembershipInvitation(journeyId, invitationId);
      await refresh();
      setMessage(zh ? "邀请已撤回；对方不会因此加入。" : "Invitation revoked. The recipient was not added.");
    } catch {
      setMessage(zh ? "邀请状态已变化或暂时无法撤回，请刷新查看。" : "The invitation changed state or could not be revoked. Refresh to review it.");
      await refresh();
    } finally {
      setBusyId(null);
    }
  };

  const openPublicMap = async () => {
    const expectedKey = mapConsentKey;
    if (!journeyId || !rendezvous || !expectedKey || navigationBusy) return;
    const popup = window.open("about:blank", "_blank");
    if (!popup) {
      setMessage(zh ? "浏览器阻止了新窗口；允许此站点打开新窗口后再试。" : "The browser blocked the new tab. Allow popups for this site and try again.");
      return;
    }
    popup.opener = null;
    setNavigationBusy(true);
    setMessage(null);
    try {
      // Bind the external release to the same plan, cursor, Place and public
      // coordinates the traveller saw before confirming consent.
      const fresh = await cityNexusApi.getJourneyOperator(journeyId);
      const freshPresence = fresh.group_presence ?? null;
      const freshTarget = freshPresence?.rendezvous ?? null;
      setGroupPresence(freshPresence);
      if (!freshTarget || groupPresenceTargetKey(freshPresence) !== expectedKey) {
        popup.close();
        setMapConsent(false);
        setMapConsentKey(null);
        setMessage(zh
          ? "计划进度或会合点刚刚变化；没有打开旧地点。请查看更新后的会合点，再重新确认。"
          : "The plan or meeting stop changed. No stale place was opened; review the updated stop and confirm again.");
        return;
      }
      const url = hasExactPublicPin(freshTarget)
        ? buildOpenStreetMapPinUrl({
            latitude: Number(freshTarget.latitude),
            longitude: Number(freshTarget.longitude),
            coordinate_system: "WGS84",
          })
        : `https://www.openstreetmap.org/search?query=${encodeURIComponent(freshTarget.place_name)}`;
      popup.location.href = url;
      setMapConsent(false);
      setMapConsentKey(null);
    } catch {
      popup.close();
      setMapConsent(false);
      setMapConsentKey(null);
      setMessage(zh
        ? "无法重新确认当前会合点；没有向地图服务发送地点。请刷新后再试。"
        : "The current meeting stop could not be revalidated; no place was sent to the map service. Refresh and try again.");
    } finally {
      setNavigationBusy(false);
    }
  };

  const requestRouteConsent = () => {
    if (!journeyId || !groupPresence?.can_guide || !onRefreshLocation) {
      setMessage(zh ? "当前没有可用于计算路线的共享会合点；公开地点回退仍可使用。" : "A shared route target is not currently available. The public-place fallback remains available.");
      return;
    }
    if (!hasExactPublicPin(rendezvous)) {
      setMessage(zh ? "会合地点缺少可靠的 WGS84 坐标，无法安全计算路线；可按名称核对公开地点。" : "The shared stop has no reliable WGS84 coordinates, so routing is unavailable. You can still verify the public place by name.");
      return;
    }
    if (!currentTargetKey) return;
    setRouteResult(null);
    setRouteConsentKey(currentTargetKey);
    setRouteConsent(true);
    setMessage(null);
  };

  const requestPersonalRoute = async () => {
    const expectedKey = routeConsentKey;
    if (!journeyId || !expectedKey || !onRefreshLocation || navigationBusy) return;
    setRouteConsent(false);
    setRouteConsentKey(null);
    setNavigationBusy(true);
    setRouteResult(null);
    setMessage(null);
    try {
      // Revalidate before asking for location, then again after the sample and
      // after routing. A changed plan/cursor/Place never receives a stale ETA.
      const before = await refreshGroupPresence();
      if (
        !before?.can_guide
        || groupPresenceTargetKey(before) !== expectedKey
        || !hasExactPublicPin(before.rendezvous)
      ) {
        setMessage(zh
          ? "计划或会合点已变化；没有读取位置。请查看新的共同会合点并重新确认。"
          : "The plan or meeting stop changed; no location was read. Review the new stop and confirm again.");
        return;
      }

      const observationId = await onRefreshLocation();
      if (!observationId) {
        setMessage(zh
          ? "没有取得这次前台位置；没有请求路线。你仍可打开公开会合点地图。"
          : "No foreground location was recorded, so no route was requested. The public meeting-place map is still available.");
        return;
      }
      const afterLocation = await refreshGroupPresence();
      if (
        !afterLocation?.can_guide
        || groupPresenceTargetKey(afterLocation) !== expectedKey
        || !hasExactPublicPin(afterLocation.rendezvous)
      ) {
        setMessage(zh
          ? "获取位置期间计划或会合点发生变化；没有计算路线。请查看更新后的目的地并重新确认。"
          : "The plan or meeting stop changed while location was being recorded. No route was calculated; review and confirm the updated destination.");
        return;
      }

      const result = await cityNexusApi.routeToGroupRendezvous(journeyId, observationId);
      const afterRoute = await refreshGroupPresence();
      const latestTarget = afterRoute?.rendezvous;
      const targetMatches = Boolean(
        afterRoute
        && latestTarget
        && result.plan_unchanged
        && result.target_unchanged
        && result.plan_version_id === afterRoute.active_plan_version_id
        && result.current_activity_id === (afterRoute.current_activity_id ?? null)
        && result.target.activity_id === latestTarget.activity_id
        && result.target.place_id === latestTarget.place_id
        && result.target.place_name === latestTarget.place_name
        && result.target.latitude === latestTarget.latitude
        && result.target.longitude === latestTarget.longitude
        && result.target.coordinate_system === latestTarget.coordinate_system
        && groupPresenceTargetKey(afterRoute) === expectedKey,
      );
      if (!targetMatches) {
        setMessage(zh
          ? "路线计算期间计划、进度或目的地已变化；旧路线已丢弃，请查看当前会合点后重新确认。"
          : "The plan, progress, or destination changed during routing. The old route was discarded; review the current stop and confirm again.");
        return;
      }
      setRouteResult({ key: expectedKey, result });
      setMessage(routeEvidenceClass(result) === "UNKNOWN"
        ? (zh ? "路线服务没有提供可验证且新鲜的路线；显示 UNKNOWN，不显示预计分钟数。" : "No fresh, verifiable route was returned. Guidance remains UNKNOWN; no duration is shown.")
        : (zh ? "路线结果已绑定到当前共享目的地；它不会改变行程或证明同行者已到达。" : "The route is bound to the current shared stop. It does not change the plan or prove that anyone has arrived."));
    } catch {
      await refreshGroupPresence();
      setMessage(zh
        ? "当前路线不可用或已过期；没有显示预计时间。公开会合点地图仍可使用。"
        : "The route is unavailable or expired; no duration is shown. The public meeting-place map remains available.");
    } finally {
      setNavigationBusy(false);
    }
  };

  const buddyMapConsentHandoff = buddyMapConsentId
    ? buddyHandoffs.find((handoff) => handoff.handoff_id === buddyMapConsentId && handoff.status === "ACCEPTED") ?? null
    : null;
  const buddyRouteConsentHandoff = buddyRouteConsent
    ? buddyHandoffs.find((handoff) => handoff.handoff_id === buddyRouteConsent.handoffId && handoff.status === "ACCEPTED") ?? null
    : null;
  const visibleRoute = routeResult?.key === currentTargetKey ? routeResult.result : null;
  const visibleRouteClass = visibleRoute ? routeEvidenceClass(visibleRoute) : null;
  return (
    <section className="journey-membership-panel" data-testid="journey-membership-panel">
      <div className="journey-membership-heading">
        <div>
          <span className="v8-kicker">{zh ? "同行者加入" : "Companion access"}</span>
          <h2>{zh ? "加入正在进行的行程" : "Join an active Journey"}</h2>
        </div>
        <button type="button" data-testid="journey-membership-refresh" onClick={() => void refresh()} disabled={busyId !== null}>
          {zh ? "刷新" : "Refresh"}
        </button>
      </div>

      {inbox.length > 0 && (
        <div className="journey-membership-inbox" aria-label={zh ? "待处理的行程邀请" : "Pending Journey invitations"}>
          {inbox.map((invitation) => (
            <article key={invitation.invitation_id} data-testid="journey-membership-invitation">
              <strong>{invitation.inviter_display_name} {zh ? "邀请你加入" : "invited you to join"} {invitation.journey_title}</strong>
              <p>
                {invitation.city_code} · {invitation.approximate_start_date} · COLLABORATOR
              </p>
              <small>
                {zh ? "仅显示行程概况和建议角色；接受前不会显示地点、成员名单或路线。邀请有效期至 " : "This preview omits Places, the member roster, and routes. Expires "}
                {new Date(invitation.expires_at).toLocaleString(locale)}
              </small>
              <div className="journey-membership-actions">
                <button
                  type="button"
                  data-testid="journey-membership-accept"
                  disabled={busyId !== null}
                  onClick={() => void respond(invitation, "ACCEPT")}
                >{zh ? "接受并加入" : "Accept and join"}</button>
                <button
                  type="button"
                  data-testid="journey-membership-decline"
                  disabled={busyId !== null}
                  onClick={() => void respond(invitation, "DECLINE")}
                >{zh ? "谢绝" : "Decline"}</button>
              </div>
            </article>
          ))}
        </div>
      )}
      {inbox.length === 0 && (
        <small>{zh ? "目前没有待处理的行程邀请。" : "You have no pending Journey invitations."}</small>
      )}

      {canManage && (
        <div className="journey-membership-invite-form">
          <label htmlFor="journey-membership-target-id">
            {zh ? "已注册旅伴的 CityNexus 账户 ID" : "Existing traveller’s CityNexus account ID"}
          </label>
          <div className="journey-membership-actions">
            <input
              id="journey-membership-target-id"
              data-testid="journey-membership-target-id"
              value={targetTravelerId}
              onChange={(event) => setTargetTravelerId(event.target.value)}
              placeholder="traveler-id"
              autoComplete="off"
            />
            <button
              type="button"
              data-testid="journey-membership-send"
              disabled={!active || !targetTravelerId.trim() || busyId !== null}
              onClick={() => void createInvitation()}
            >{busyId === "create" ? (zh ? "发送中…" : "Sending…") : (zh ? "邀请为同行者" : "Invite as companion")}</button>
          </div>
          <small>
            {zh
              ? "邀请仅授予 COLLABORATOR。只有收件人本人签名接受后才会加入；邀请 24 小时后失效。"
              : "The invitation offers COLLABORATOR only. Membership begins after the recipient’s own signed acceptance; the invitation expires in 24 hours."}
          </small>
          {!active && <small>{zh ? "此 Journey 尚未开始或没有当前计划，暂不能邀请加入。" : "This Journey must be active with a selected plan before inviting a companion."}</small>}
          {sent.filter((item) => item.state === "OFFERED").map((invitation) => (
            <article key={invitation.invitation_id} data-testid="journey-membership-sent-invitation">
              <span>{invitation.target_display_name} · {zh ? "待接受" : "Awaiting acceptance"}</span>
              <button
                type="button"
                disabled={busyId !== null}
                onClick={() => void revoke(invitation.invitation_id)}
              >{zh ? "撤回" : "Revoke"}</button>
            </article>
          ))}
        </div>
      )}

      {journeyId && active && (
        <div className="journey-membership-catchup" data-testid="journey-membership-catchup">
          <strong>{recentlyJoinedJourneyId === journeyId
            ? (zh ? "如何追上同行者？" : "How do I catch up?")
            : (zh ? "下一处共同会合点" : "Next shared meeting stop")}</strong>
          {rendezvous ? (
            <>
              <p>{zh ? `当前计划指定的会合地点：${rendezvous.place_name}。` : `Plan-selected meeting stop: ${rendezvous.place_name}.`}</p>
              <small>{hasExactPublicPin(rendezvous)
                ? (zh ? "地图使用当前共享计划中的 WGS84 公共地点坐标，不使用任何成员的实时位置。" : "Map pin comes from the shared active-plan public Place (WGS84), not a member's live location.")
                : (zh ? "没有可靠的公共地点坐标。地图只能按名称搜索，可能出现多个同名地点，出发前请核对。" : "No reliable public destination coordinates are available. Map search by name may be ambiguous; verify the place before traveling.")}</small>
              <small data-testid="journey-membership-location-truth">{visibleRoute
                ? visibleRouteClass === "UNKNOWN"
                  ? (zh
                    ? "本次路线请求没有得到可靠 ETA；你自愿提交的前台位置观察可能已保存在 Journey 中。"
                    : "This route request did not establish a trustworthy ETA. Your consented foreground location observation may already be stored on this Journey.")
                  : (zh
                    ? "下方路线来自你主动授权的检查；前台位置观察可能保留在 Journey，旅伴可能看到变化后的粗粒度队伍状态。"
                    : "The route below follows your explicit location consent. The foreground observation may remain on this Journey and can change companions' coarse group-presence view.")
                : (zh
                  ? "如需个人 ETA，请主动授权新的前台位置检查；此前提交的 Journey 位置观察可能仍被保存，不会因为本页未请求路线就自动删除。"
                  : "To check a personal ETA, explicitly authorize a new foreground location sample. Earlier Journey observations may still be retained even when this panel has no route result.")}</small>
              <button type="button" onClick={() => {
                setMapConsentKey(currentTargetKey);
                setMapConsent(true);
              }}>
                {hasExactPublicPin(rendezvous)
                  ? (zh ? "打开会合点精确地图…" : "Open exact meeting-place pin…")
                  : (zh ? "按名称查找会合点（需核对）…" : "Search for meeting place (verify result)…")}
              </button>
              {groupPresence?.can_guide && onRefreshLocation && (
                <button
                  type="button"
                  data-testid="journey-membership-route-check"
                  disabled={navigationBusy}
                  onClick={requestRouteConsent}
                >{zh ? "检查我到下一处会合点的路线…" : "Check my route to our next stop…"}</button>
              )}
              {groupPresence?.can_guide && (
                <section data-testid="journey-rendezvous-coordination">
                  <strong>{zh ? "为这个共同会合点协调" : "Coordinate around this shared stop"}</strong>
                  <small>
                    {zh
                      ? "路线检查与状态分享是分开的。只有你明确确认后，当前同行者才会看到这条自报状态；它不代表真实到场。"
                      : "Route checks and status sharing are separate. Current companions see a status only after your explicit confirmation; it is not proof of arrival."}
                  </small>
                  <div className="journey-membership-actions">
                    <button
                      type="button"
                      data-testid="journey-rendezvous-heading-share"
                      disabled={coordinationBusy}
                      onClick={() => openCoordinationConsent("HEADING_TO_RENDEZVOUS")}
                    >{zh ? "告诉同行者我正在赶往会合点" : "Tell companions I'm on my way"}</button>
                    <button
                      type="button"
                      data-testid="journey-rendezvous-help-share"
                      disabled={coordinationBusy}
                      onClick={() => openCoordinationConsent("NEEDS_COORDINATION")}
                    >{zh ? "我需要帮助协调" : "I need help coordinating"}</button>
                    <button
                      type="button"
                      data-testid="journey-rendezvous-refresh"
                      disabled={coordinationBusy}
                      onClick={() => void refreshGroupPresence()}
                    >{zh ? "刷新团队状态" : "Refresh team status"}</button>
                  </div>
                  {coordinationSignals.length > 0 ? (
                    <div data-testid="journey-rendezvous-status-list">
                      {coordinationSignals.map((signal) => (
                        <article key={signal.signal_id} data-testid="journey-rendezvous-status" data-signal-id={signal.signal_id}>
                          <strong>{coordinationStatusLabel(signal, zh)}</strong>
                          <small>
                            {zh
                              ? `会合点：${signal.public_place_name}。自报时间：${new Date(signal.reported_at).toLocaleString(locale)}。有效至：${new Date(signal.expires_at).toLocaleString(locale)}。`
                              : `Stop: ${signal.public_place_name}. Self-reported ${new Date(signal.reported_at).toLocaleString(locale)}; expires ${new Date(signal.expires_at).toLocaleString(locale)}.`}
                          </small>
                          <small>{zh ? "自报意向 · 未验证，不含 GPS 起点或个人路线，也不表示已到场。" : "Self-reported and unverified; no GPS origin or private route is shared, and this does not mean arrival."}</small>
                          {signal.acknowledgement && (
                            <small data-testid="journey-rendezvous-help-acknowledgement">
                              {zh
                                ? `${signal.acknowledgement.display_name} 已用本人身份回应这次求助；这不是现场等待或会合确认。`
                                : `${signal.acknowledgement.display_name} acknowledged this request with their own identity; this is not a promise to wait there or an arrival confirmation.`}
                            </small>
                          )}
                          {signal.signal_kind === "NEEDS_COORDINATION"
                            && signal.viewer_is_acknowledger
                            && signal.acknowledgement
                            && !buddyHandoffs.some((handoff) => handoff.help_signal_id === signal.signal_id)
                            && (
                              <button
                                type="button"
                                data-testid="journey-buddy-load-candidates"
                                disabled={buddyActionBusy}
                                onClick={() => void loadBuddyHandoffCandidates(signal)}
                              >{zh ? "提议一个公开接应地点" : "Offer a public meeting point"}</button>
                            )}
                          {buddyCandidates?.help_signal_id === signal.signal_id && (
                            <section data-testid="journey-buddy-candidate-chooser">
                              <strong>{zh ? "选择一个双方可见的公共地点" : "Choose a public place for this private offer"}</strong>
                              <p>{zh
                                ? "这些地点来自当前会合点或服务器目录。直线距离不是路线或 ETA；开放时间、无障碍与可达性均为 UNKNOWN，实际可行性未验证。接应意向不会改变行程，也不要因此放弃受保护承诺。"
                                : "Choices come from the current stop or server catalog. Straight-line distance is not a route or ETA; opening hours, accessibility, and reachability are UNKNOWN, and feasibility is unverified. This intention does not change the Journey; do not abandon protected commitments."}</p>
                              <label htmlFor="journey-buddy-candidate-select">{zh ? "接应地点" : "Public handoff place"}</label>
                              <select
                                id="journey-buddy-candidate-select"
                                data-testid="journey-buddy-candidate-select"
                                value={buddySelectedPlaceId}
                                onChange={(event) => setBuddySelectedPlaceId(event.target.value)}
                              >
                                {buddyCandidates.items.map((place) => (
                                  <option key={place.place_id} value={place.place_id}>
                                    {place.name} · {place.distance_from_original_m} m straight-line · {place.source_label}
                                  </option>
                                ))}
                              </select>
                              {buddyCandidates.items.find((place) => place.place_id === buddySelectedPlaceId) && (
                                <small data-testid="journey-buddy-candidate-provenance">
                                  {buddyCandidates.items.find((place) => place.place_id === buddySelectedPlaceId)?.source_label}
                                  {zh ? "；路线、开放时间和无障碍状态均未知。" : "; route, opening hours, and accessibility are UNKNOWN."}
                                </small>
                              )}
                              <label htmlFor="journey-buddy-execution-window">
                                {zh ? "双方同意后的接应执行时长" : "Bounded assist window after both accept"}
                              </label>
                              <select
                                id="journey-buddy-execution-window"
                                data-testid="journey-buddy-execution-window"
                                value={buddyExecutionWindowMinutes}
                                onChange={(event) => setBuddyExecutionWindowMinutes(Number(event.target.value) as 30 | 60 | 90)}
                              >
                                {[30, 60, 90].map((minutes) => (
                                  <option key={minutes} value={minutes}>{minutes} minutes</option>
                                ))}
                              </select>
                              <small>
                                {zh
                                  ? "此时长与地点一起提交；20 分钟是提议接受期限。只有求助者本人接受后，执行窗口才开始，不会自动续期。"
                                  : "This duration is offered with the place. The 20-minute limit applies to accepting the offer; the execution window starts only after the requester accepts and never auto-renews."}
                              </small>
                              <div className="journey-membership-actions">
                                <button
                                  type="button"
                                  data-testid="journey-buddy-offer"
                                  disabled={buddyActionBusy || !buddySelectedPlaceId}
                                  onClick={() => void offerBuddyHandoff()}
                                >{zh ? "向求助者发送此地点" : "Offer this place to the requester"}</button>
                                <button
                                  type="button"
                                  data-testid="journey-buddy-candidate-cancel"
                                  disabled={buddyActionBusy}
                                  onClick={() => {
                                    setBuddyCandidates(null);
                                    setBuddySelectedPlaceId("");
                                  }}
                                >{zh ? "取消" : "Cancel"}</button>
                              </div>
                            </section>
                          )}
                          {signal.viewer_can_acknowledge && (
                            <button
                              type="button"
                              data-testid="journey-rendezvous-help-acknowledge"
                              disabled={coordinationBusy}
                              onClick={() => void acknowledgeCoordinationHelp(signal)}
                            >{zh ? "我可以帮忙协调" : "I can help coordinate"}</button>
                          )}
                          {signal.viewer_can_withdraw && (
                            <button
                              type="button"
                              data-testid="journey-rendezvous-withdraw"
                              disabled={coordinationBusy}
                              onClick={() => void withdrawCoordinationSignal()}
                            >{zh ? "停止分享此状态" : "Stop sharing this status"}</button>
                          )}
                        </article>
                      ))}
                    </div>
                  ) : (
                    <small data-testid="journey-rendezvous-no-status">
                      {zh ? "目前没有同行者主动分享此会合点状态。" : "No companion has voluntarily shared a status for this stop."}
                    </small>
                  )}
                  {buddyHandoffs.length > 0 && (
                    <div data-testid="journey-buddy-handoffs">
                      {buddyHandoffs.map((handoff) => {
                        const privateRoute = buddyPrivateRoutes[handoff.handoff_id];
                        const outboundLeg = privateRoute?.outbound_leg ?? null;
                        const returnLeg = privateRoute?.return_leg ?? null;
                        const legHasUsableEvidence = (leg: typeof outboundLeg) => Boolean(
                          leg
                          && (leg.route_state === "LIVE" || leg.route_state === "REPLAY")
                          && typeof leg.duration_min === "number"
                          && Number.isFinite(leg.duration_min)
                          && leg.duration_min >= 0
                          && leg.provider
                          && leg.provenance,
                        );
                        const topLevelRouteUsable = privateRoute?.route_state === "LIVE" || privateRoute?.route_state === "REPLAY";
                        const handoffIsOriginalSharedStop =
                          handoff.handoff_place.place_id === handoff.original_shared_stop.place_id;
                        // The exact original shared stop needs NO separate second leg.
                        // R81 marks that leg SAME_PUBLIC_DESTINATION with 0 minutes;
                        // don't hide a valid caller-private outbound ETA in this case.
                        const returnSatisfied = handoffIsOriginalSharedStop
                          ? returnLeg?.route_state === "SAME_PUBLIC_DESTINATION" && returnLeg.duration_min === 0
                          : legHasUsableEvidence(returnLeg);
                        const hasBothUsableBuddyLegs = legHasUsableEvidence(outboundLeg)
                          && returnSatisfied
                          && topLevelRouteUsable;
                        const routeIsStale = privateRoute?.status === "STALE" || privateRoute?.route_state === "STALE";
                        const displayedRouteState = hasBothUsableBuddyLegs
                          ? privateRoute?.route_state ?? "UNKNOWN"
                          : routeIsStale
                            ? "STALE"
                            : privateRoute?.route_state === "TEST_PROVIDER_EMULATOR"
                              ? "TEST_PROVIDER_EMULATOR"
                              : "UNKNOWN";
                        const risk = privateRoute?.shared_commitment_risk;
                        const oneRiskLeg = risk?.legs_used?.length === 1 ? risk.legs_used[0] : null;
                        const oneRiskLegIsLive = oneRiskLeg === "TO_HANDOFF"
                          ? outboundLeg?.route_state === "LIVE" && legHasUsableEvidence(outboundLeg)
                          : oneRiskLeg === "RETURN_TO_SHARED_STOP"
                            ? returnLeg?.route_state === "LIVE" && legHasUsableEvidence(returnLeg)
                            : false;
                        const oneVerifiedLegAloneAtRisk = risk?.state === "COMMITMENT_AT_RISK"
                          && risk.basis === "ONE_VERIFIED_ROUTE_LEG_ALONE_EXCEEDS_PROTECTED_COMMITMENT"
                          && oneRiskLegIsLive;
                        const displayedCommitmentRisk = hasBothUsableBuddyLegs
                          ? risk?.state ?? "UNKNOWN"
                          : oneVerifiedLegAloneAtRisk ? "COMMITMENT_AT_RISK" : "UNKNOWN";
                        const execution = handoff.execution ?? null;
                        const returnDirectionsButton = execution?.can_return_to_shared_stop ? (
                          <button
                            type="button"
                            data-testid="journey-buddy-return-open"
                            disabled={buddyActionBusy || !currentTargetKey}
                            onClick={() => beginBuddyReturnDirectionsConsent(handoff)}
                          >{zh ? "单独同意后导航回当前共同会合点…" : "Navigate back to the current shared stop with separate consent…"}</button>
                        ) : null;
                        return (
                        <article key={handoff.handoff_id} data-testid="journey-buddy-handoff" data-handoff-id={handoff.handoff_id}>
                          <strong>{handoff.status === "ACCEPTED"
                            ? (zh ? "双方已接受公开接应意向" : "Mutually accepted public handoff intention")
                            : (zh ? "收到一处公开接应地点提议" : "Public handoff place offered")}</strong>
                          <p>{handoff.helper_display_name} → {handoff.requester_display_name}: {handoff.handoff_place.name}</p>
                          <small>{zh
                            ? `${handoff.handoff_place.source_label} · 距原会合点直线 ${handoff.handoff_place.distance_from_original_m} 米（不是路线或 ETA）。`
                            : `${handoff.handoff_place.source_label} · ${handoff.handoff_place.distance_from_original_m} m straight-line from the original stop, not a route or ETA.`}</small>
                          <small>{zh
                            ? `提议有效至 ${new Date(handoff.expires_at).toLocaleString(locale)}。路线、开放时间、无障碍状态均为 UNKNOWN，实际可行性未验证；这不是实时位置或到场证明。原共同会合点“${handoff.original_shared_stop.place_name}”和整个计划保持不变，请勿因此放弃受保护承诺。`
                            : `Offer expires ${new Date(handoff.expires_at).toLocaleString(locale)}. Route, opening hours, and accessibility are UNKNOWN; feasibility is unverified. This is not live location or proof of arrival. The original shared stop “${handoff.original_shared_stop.place_name}” and Journey remain unchanged; do not abandon protected commitments.`}</small>
                          {execution?.status === "ACTIVE" && execution.context_current && (
                            <small data-testid="journey-buddy-active-lease-truth">
                              {zh
                                ? `旧接应提议的接受期限是 ${new Date(handoff.expires_at).toLocaleString(locale)}；它不是现在双方已同意的执行截止时间。有效执行窗口截止 ${execution.expires_at ? new Date(execution.expires_at).toLocaleString(locale) : "未知"}。接应不会自动续期。`
                                : `The old handoff offer's acceptance deadline was ${new Date(handoff.expires_at).toLocaleString(locale)}. That is NOT the expiry of the mutually agreed execution, which runs until ${execution.expires_at ? new Date(execution.expires_at).toLocaleString(locale) : "unknown"}. There is no automatic renewal.`}
                            </small>
                          )}
                          {handoff.status === "OFFERED" && execution?.status === "WINDOW_OFFERED" && (
                            <small data-testid="journey-buddy-window-offer-truth">
                              {zh
                                ? `接受地点也表示你同意 ${execution.execution_window_minutes} 分钟的接应执行窗口；只有你本人接受后才开始，不会自动续期。`
                                : `Accepting this place also accepts a ${execution.execution_window_minutes}-minute assist window. It starts only after your own acceptance and never renews automatically.`}
                            </small>
                          )}
                          {handoff.status === "OFFERED" && handoff.viewer_role === "REQUESTER" && (
                            <div className="journey-membership-actions">
                              <button type="button" data-testid="journey-buddy-accept" disabled={buddyActionBusy}
                                onClick={() => void respondToBuddyHandoff(handoff, "ACCEPT")}>
                                {execution?.status === "WINDOW_OFFERED"
                                  ? (zh
                                    ? `接受地点与 ${execution.execution_window_minutes} 分钟执行窗口`
                                    : `Accept place and ${execution.execution_window_minutes}-minute window`)
                                  : (zh ? "接受此接应地点" : "Accept this handoff place")}
                              </button>
                              <button type="button" data-testid="journey-buddy-decline" disabled={buddyActionBusy}
                                onClick={() => void respondToBuddyHandoff(handoff, "DECLINE")}>
                                {zh ? "拒绝" : "Decline"}
                              </button>
                            </div>
                          )}
                          {handoff.status === "ACCEPTED" && (
                            <>
                              <small data-testid="journey-buddy-accepted-truth">
                                {zh ? "这是双方同意的公共会合意向，不是 GPS 证明；双方各自决定是否导航。" : "This is a mutually agreed public-place intention, not GPS proof. Each person chooses whether to navigate."}
                              </small>
                              {execution?.status === "WINDOW_OFFERED" && handoff.viewer_role === "REQUESTER" && execution.can_accept_window && (
                                <section data-testid="journey-buddy-window-acceptance">
                                  <p>{zh
                                    ? `对方单独同意了 ${execution.execution_window_minutes} 分钟执行窗口。提议仍受原 20 分钟接受期限约束；由你签名接受后才开始计时。`
                                    : `The helper separately offered a ${execution.execution_window_minutes}-minute execution window. The proposal remains subject to the original 20-minute deadline; its clock starts only when you sign to accept.`}</p>
                                  <button
                                    type="button"
                                    data-testid="journey-buddy-execution-window-accept"
                                    disabled={buddyActionBusy}
                                    onClick={() => void acceptBuddyExecutionWindow(handoff)}
                                  >{zh ? `接受 ${execution.execution_window_minutes} 分钟执行窗口` : `Accept ${execution.execution_window_minutes}-minute window`}</button>
                                </section>
                              )}
                              {handoff.execution_window_offer_available && handoff.viewer_role === "HELPER" && (
                                <section data-testid="journey-buddy-window-upgrade">
                                  <p>{zh
                                    ? "这是先前版本接受的接应。提议执行窗口会单独征求对方同意；不会追溯延长旧协议。"
                                    : "This handoff was accepted by an earlier version. Offer a separate execution window; it will not retroactively extend the old agreement."}</p>
                                  <label htmlFor={`journey-buddy-upgrade-window-${handoff.handoff_id}`}>
                                    {zh ? "提议执行时长" : "Proposed execution window"}
                                  </label>
                                  <select
                                    id={`journey-buddy-upgrade-window-${handoff.handoff_id}`}
                                    data-testid="journey-buddy-upgrade-window-select"
                                    value={buddyExecutionWindowMinutes}
                                    onChange={(event) => setBuddyExecutionWindowMinutes(Number(event.target.value) as 30 | 60 | 90)}
                                  >
                                    {[30, 60, 90].map((minutes) => (
                                      <option key={minutes} value={minutes}>{minutes} minutes</option>
                                    ))}
                                  </select>
                                  <button
                                    type="button"
                                    data-testid="journey-buddy-execution-window-offer"
                                    disabled={buddyActionBusy}
                                    onClick={() => void offerBuddyExecutionWindow(handoff, buddyExecutionWindowMinutes)}
                                  >{zh ? "单独提议此执行时长" : "Offer this bounded window separately"}</button>
                                </section>
                              )}
                              {execution?.status === "WINDOW_OFFERED" && handoff.viewer_role === "HELPER" && handoff.status === "ACCEPTED" && (
                                <small data-testid="journey-buddy-window-waiting">
                                  {zh ? `已提出 ${execution.execution_window_minutes} 分钟窗口；等待求助者本人签名接受，计时尚未开始。` : `The ${execution.execution_window_minutes}-minute window is offered. Waiting for the requester’s own signed acceptance; its clock has not started.`}
                                </small>
                              )}
                              {execution?.status === "ACTIVE" && (
                                <section data-testid="journey-buddy-execution-active">
                                  <strong>{zh ? "双方同意的接应正在执行" : "Mutually agreed buddy assist in progress"}</strong>
                                  <small>{zh
                                    ? `有限窗口：${execution.execution_window_minutes} 分钟；开始 ${execution.started_at ? new Date(execution.started_at).toLocaleString(locale) : "—"}；截止 ${execution.expires_at ? new Date(execution.expires_at).toLocaleString(locale) : "—"}。不会后台追踪或自动续期。`
                                    : `Bounded window: ${execution.execution_window_minutes} minutes; started ${execution.started_at ? new Date(execution.started_at).toLocaleString(locale) : "—"}; expires ${execution.expires_at ? new Date(execution.expires_at).toLocaleString(locale) : "—"}. No background tracking or auto-renewal.`}</small>
                                  <small data-testid="journey-buddy-execution-confirmations">
                                    {execution.helper_confirmed
                                      ? (zh ? `${handoff.helper_display_name} 已本人确认。` : `${handoff.helper_display_name} confirmed personally.`)
                                      : (zh ? `${handoff.helper_display_name} 尚未确认。` : `${handoff.helper_display_name} has not confirmed.`)}
                                    {" "}
                                    {execution.requester_confirmed
                                      ? (zh ? `${handoff.requester_display_name} 已本人确认。` : `${handoff.requester_display_name} confirmed personally.`)
                                      : (zh ? `${handoff.requester_display_name} 尚未确认。` : `${handoff.requester_display_name} has not confirmed.`)}
                                    {execution.viewer_receipt_kind === "BUDDY_MET_ACK" && !execution.partner_receipt_kind
                                      ? (zh ? "等待对方独立确认。" : "Waiting for the other member’s independent confirmation.")
                                      : ""}
                                  </small>
                                  <small>{zh
                                    ? "下列按钮只记录你本人签名自报；不会因 GPS、路线、距离或地图操作自动完成，也不是设备验证的到场证明。"
                                    : "These buttons record only your signed self-report. GPS, routes, proximity, and map actions never complete the assist, and the report is not device-verified arrival."}</small>
                                  {execution.can_report && (
                                    <div className="journey-membership-actions">
                                      <button
                                        type="button"
                                        data-testid="journey-buddy-met-ack"
                                        disabled={buddyActionBusy}
                                        onClick={() => void reportBuddyExecution(handoff, execution, "BUDDY_MET_ACK")}
                                      >{zh ? "我在约定地点看到伙伴，确认接应协作完成" : "I am at the agreed place and can see my buddy"}</button>
                                      <button
                                        type="button"
                                        data-testid="journey-buddy-cannot-meet"
                                        disabled={buddyActionBusy}
                                        onClick={() => void reportBuddyExecution(handoff, execution, "CANNOT_MEET")}
                                      >{zh ? "我无法在这里接应，需要重新协调" : "I cannot meet here; I need to replan"}</button>
                                      <button
                                        type="button"
                                        data-testid="journey-buddy-cancel-participation"
                                        disabled={buddyActionBusy}
                                        onClick={() => void reportBuddyExecution(handoff, execution, "CANCEL_PARTICIPATION")}
                                      >{zh ? "退出我自己的接应参与" : "Cancel my participation"}</button>
                                    </div>
                                  )}
                                </section>
                              )}
                              {execution?.status === "MUTUALLY_ACKNOWLEDGED" && (
                                <section data-testid="journey-buddy-mutual-outcome">
                                  <strong>{zh ? "双方已独立确认接应协作完成" : "Both members independently confirmed the assist"}</strong>
                                  <small>{zh
                                    ? "这是双方签名自报的共同结果，不是 GPS、距离或设备验证的物理会合；不关闭 R62/R69，不修改共享计划。"
                                    : "This is a mutually signed self-reported outcome, not GPS, distance, or device-verified physical reunion. It does not close R62/R69 or change the shared plan."}</small>
                                  {returnDirectionsButton}
                                </section>
                              )}
                              {execution?.status === "REPLAN_REQUIRED" && (
                                <section data-testid="journey-buddy-replan-required">
                                  <strong>{zh ? "此接应无法按原约定完成" : "This handoff cannot finish as agreed"}</strong>
                                  <small>{zh ? "没有记录为双方完成。你可以发起新的、单独授权的帮助请求，或导航回当前共同会合点。" : "It was not recorded as mutually completed. You can make a fresh, separately consented help request or navigate to the current shared stop."}</small>
                                  <button type="button" data-testid="journey-buddy-replan-help" disabled={coordinationBusy}
                                    onClick={() => openCoordinationConsent("NEEDS_COORDINATION")}>
                                    {zh ? "请求重新协调帮助…" : "Request fresh help…"}
                                  </button>
                                  {returnDirectionsButton}
                                </section>
                              )}
                              {execution?.status === "CANCELLED" && (
                                <section data-testid="journey-buddy-execution-cancelled">
                                  <small>{zh ? "一位成员退出了自己的参与；没有推断另一人的确认。" : "A member cancelled their own participation; no confirmation was inferred for the other person."}</small>
                                  {returnDirectionsButton}
                                </section>
                              )}
                              {execution?.status === "EXPIRED" && (
                                <section data-testid="journey-buddy-execution-expired">
                                  <small>{zh ? "双方接应执行窗口已到期，系统没有自动续期。需要重新同意才能继续接应。" : "The pair execution window expired and was not renewed. A new mutual agreement is required to continue the assist."}</small>
                                  <button type="button" data-testid="journey-buddy-replan-help" disabled={coordinationBusy}
                                    onClick={() => openCoordinationConsent("NEEDS_COORDINATION")}>
                                    {zh ? "重新协调帮助…" : "Replan help…"}
                                  </button>
                                  {returnDirectionsButton}
                                </section>
                              )}
                              {execution?.status === "STALE" && (
                                <section data-testid="journey-buddy-execution-stale">
                                  <small>{zh ? `原接应上下文已暂停（${execution.stale_reason ?? "需要重新核对"}）；刷新当前计划并重新取得双方同意。` : `The original handoff context is paused (${execution.stale_reason ?? "revalidation required"}); refresh the current plan and obtain consent again.`}</small>
                                  <button type="button" data-testid="journey-buddy-replan-help" disabled={coordinationBusy}
                                    onClick={() => openCoordinationConsent("NEEDS_COORDINATION")}>
                                    {zh ? "按当前计划重新协调帮助…" : "Request fresh help for the current plan…"}
                                  </button>
                                </section>
                              )}
                              <button type="button" data-testid="journey-buddy-map-open"
                                disabled={buddyActionBusy || !buddyHandoffCanNavigate(handoff)}
                                onClick={() => setBuddyMapConsentId(handoff.handoff_id)}>
                                {zh ? "打开接应地点精确地图…" : "Open exact handoff map pin…"}
                              </button>
                              <button
                                type="button"
                                data-testid="journey-buddy-directions-open"
                                disabled={buddyActionBusy || !buddyHandoffCanNavigate(handoff) || !currentTargetKey || !currentAudienceKey}
                                onClick={() => beginBuddyDirectionsConsent(handoff)}
                              >{zh ? "单独同意后打开外部步行导航…" : "Open external walking directions with separate consent…"}</button>
                              {onRefreshLocation && (
                                <button
                                  type="button"
                                  data-testid="journey-buddy-route-start"
                                  disabled={buddyActionBusy || buddyRouteBusy || !buddyHandoffCanNavigate(handoff)}
                                  onClick={() => beginBuddyRouteCheck(handoff)}
                                >{zh ? "检查我到这处接应点的步行路线…" : "Check MY walking route to this handoff…"}</button>
                              )}
                              {privateRoute && (
                                <article
                                  data-testid="journey-buddy-route-result"
                                  data-route-state={displayedRouteState}
                                  data-handoff-id={handoff.handoff_id}
                                >
                                  <strong>{displayedRouteState === "LIVE"
                                    ? (zh ? "当前路线服务结果（仅你可见）" : "Current route-provider result (only you can see it)")
                                    : displayedRouteState === "REPLAY"
                                      ? (zh ? "历史路线回放，不是当前路况" : "Historical route replay, not current traffic")
                                      : displayedRouteState === "STALE"
                                        ? (zh ? "路线上下文已过期；没有可用预计时间" : "Route context is stale; no actionable estimate")
                                      : displayedRouteState === "TEST_PROVIDER_EMULATOR"
                                        ? (zh ? "仅测试路线服务结果，不是实时路线证据" : "Test-provider output only; not live route evidence")
                                      : (zh ? "路线状态：UNKNOWN / ROUTE_UNAVAILABLE" : "Route status: UNKNOWN / ROUTE_UNAVAILABLE")}</strong>
                                  <p>{zh
                                    ? `目的地：${privateRoute.target_place_name}（${privateRoute.target_source_label}）。原共同会合点“${privateRoute.original_shared_stop_name}”仍是全体计划地点；到接应点不代表到达原会合点。`
                                    : `Destination: ${privateRoute.target_place_name} (${privateRoute.target_source_label}). The original shared stop “${privateRoute.original_shared_stop_name}” remains the whole group's planned meeting point; reaching this handoff does not mean reaching that stop.`}</p>
                                  <div data-testid="journey-buddy-route-legs">
                                    {([
                                      ["outbound", outboundLeg, zh ? "本人到接应点" : "Your origin to the handoff"],
                                      ["return", returnLeg, zh ? "接应点回原共同会合点" : "Handoff to the original shared stop"],
                                    ] as const).map(([legKey, leg, label]) => (
                                      <section
                                        key={legKey}
                                        data-testid="journey-buddy-route-leg"
                                        data-leg={legKey}
                                        data-route-state={leg?.route_state ?? "UNKNOWN"}
                                      >
                                        <strong>{label}: {leg?.route_state ?? "UNKNOWN"}</strong>
                                        <small>
                                          {leg?.provider ?? (zh ? "路线服务未配置或未提供" : "provider unavailable")}
                                          {" · "}{leg?.provenance ?? (zh ? "没有独立路线证据" : "no separate leg evidence")}
                                          {leg?.reason ? ` · ${leg.reason}` : ""}
                                        </small>
                                        {leg?.route_state === "TEST_PROVIDER_EMULATOR" ? (
                                          <small>{zh ? "仅受控测试时长，不是实际路线或 ETA。" : "Controlled test duration only; not a real route or ETA."}</small>
                                        ) : leg?.duration_min !== null && leg?.duration_min !== undefined
                                          && (leg.route_state === "LIVE" || leg.route_state === "REPLAY") ? (
                                            <small>{zh ? `本段约 ${leg.duration_min} 分钟。` : `Leg estimate: about ${leg.duration_min} minutes.`}</small>
                                          ) : (
                                            <small>{zh ? "本段时间未知；不会用另一段补齐。" : "This leg's duration is unknown; the other leg does not fill it in."}</small>
                                          )}
                                      </section>
                                    ))}
                                  </div>
                                  {privateRoute.duration_min !== null && hasBothUsableBuddyLegs && displayedRouteState !== "UNKNOWN" ? (
                                    <small>{handoffIsOriginalSharedStop
                                      ? (zh
                                        ? `接应点就是原共同会合点，本人步行约 ${privateRoute.duration_min} 分钟，无需第二段路线。`
                                        : `The handoff is already the original shared stop. Your walking estimate is about ${privateRoute.duration_min} minutes; no second leg is required.`)
                                      : (zh
                                        ? `两段移动时间合计约 ${privateRoute.duration_min} 分钟（不含接应停留时间）。`
                                        : `Both travel legs total about ${privateRoute.duration_min} minutes (excluding time spent at the handoff).`)}</small>
                                  ) : (
                                    <small>{zh ? "两段路线未全部得到可用证据；总时间与承诺安全余量保持 UNKNOWN。可单独同意打开外部目的地导航。" : "Both legs lack usable evidence; total time and commitment slack remain UNKNOWN. You may separately consent to external destination-only directions."}</small>
                                  )}
                                  <small>
                                    {zh ? "来源：" : "Source: "}{privateRoute.provider ?? (zh ? "未配置" : "not configured")}
                                    {" · "}{zh ? "证据：" : "Evidence: "}{privateRoute.provenance}
                                    {privateRoute.captured_at && (
                                      <>{" · "}{zh ? "回执时间：" : "Receipt time: "}{new Date(privateRoute.captured_at).toLocaleString(locale)}</>
                                    )}
                                    {privateRoute.origin_age_seconds !== null && (
                                      <>{" · "}{zh ? "本人的位置观察年龄：" : "your observation age: "}{privateRoute.origin_age_seconds}s</>
                                    )}
                                  </small>
                                  <small>{zh ? "受保护承诺风险：" : "Protected-commitment risk: "}{displayedCommitmentRisk}
                                    {displayedCommitmentRisk === "UNKNOWN" && handoff.handoff_place.place_id !== handoff.original_shared_stop.place_id
                                      && returnLeg?.route_state !== "LIVE"
                                      ? (zh ? "；从接应点到原共同会合点的可靠路线未知，因此不会显示安全余量。" : "; the onward route to the original shared stop is not live-verified, so no safe buffer is claimed.")
                                      : ""}</small>
                                  {risk?.basis === "TWO_LEG_TRAVEL_ONLY_HANDOFF_DWELL_UNKNOWN" && (
                                    <small data-testid="journey-buddy-dwell-unknown">
                                      {zh
                                        ? "两段路线只计算移动时间；实际接应、等待、沟通需要的时间未知，不能因此证明能准时返回原共同承诺。"
                                        : "Both route legs count travel time only. Time spent meeting, waiting, or helping is unknown; these estimates do not prove a safe return to the original commitment."}
                                    </small>
                                  )}
                                  {displayedCommitmentRisk === "COMMITMENT_AT_RISK" && (
                                    <strong role="alert" data-testid="journey-buddy-route-commitment-warning">
                                      {oneVerifiedLegAloneAtRisk
                                        ? (zh
                                          ? "一段已验证路线单独就足以表明可能错过受保护承诺。下一步：返回原共同会合点，或请求获授权的团队恢复；此接应点不代表已重新加入行程。"
                                          : "One verified route leg alone exceeds the protected commitment. Next: return to the original shared stop or request authorized group recovery; this handoff does not mean you rejoined the Journey.")
                                        : (zh
                                          ? "两段路线显示可能错过受保护承诺。下一步：返回原共同会合点，或请求获授权的团队恢复；此接应点不代表已重新加入行程。"
                                          : "The two legs indicate that you may miss the protected commitment. Next: return to the original shared stop or request authorized group recovery; this handoff does not mean you rejoined the Journey.")}
                                    </strong>
                                  )}
                                  <small>{zh
                                    ? "路线、位置观察 ID 和精确起点只返回给当前本人；不会共享给另一位成员或 B。路线结果不证明已出发、到场或见面；本请求不会修改计划或触发 Guardian。"
                                    : "The route, observation ID, and precise origin are returned only to you, never to the other member or B. A route is not proof of departure, arrival, or reunion; this request does not change the plan or trigger Guardian."}</small>
                                </article>
                              )}
                            </>
                          )}
                          {(!execution || execution.status === "WINDOW_OFFERED") && (
                            <button type="button" data-testid="journey-buddy-close" disabled={buddyActionBusy}
                              onClick={() => void closeBuddyHandoff(handoff)}>
                              {handoff.viewer_role === "HELPER"
                                ? (zh ? "取消接应提议" : "Cancel handoff")
                                : (zh ? "撤回我的接应同意" : "Revoke my handoff agreement")}
                            </button>
                          )}
                        </article>
                        );
                      })}
                    </div>
                  )}
                  {coordinationMessage && (
                    <small role="status" data-testid="journey-rendezvous-message">{coordinationMessage}</small>
                  )}
                  {buddyMessage && <small role="status" data-testid="journey-buddy-message">{buddyMessage}</small>}
                  {buddyRouteMessage && <small role="status" data-testid="journey-buddy-route-message">{buddyRouteMessage}</small>}
                  {coordinationConsentKind && (
                    <section
                      role="dialog"
                      aria-modal="true"
                      aria-labelledby="journey-rendezvous-consent-title"
                      data-testid="journey-rendezvous-consent"
                    >
                      <strong id="journey-rendezvous-consent-title">
                        {coordinationConsentKind === "HEADING_TO_RENDEZVOUS"
                          ? (zh ? "确认分享正在前往的自报状态？" : "Confirm sharing your self-reported intention?")
                          : (zh ? "确认向同行者请求协调帮助？" : "Confirm asking companions for coordination help?")}
                      </strong>
                      <p>
                        {zh
                          ? `当前会看到的同行者：${groupPresence.members.filter((item) => !item.is_viewer).map((item) => item.display_name).join("、")}。对象仅是当前共享计划的公共会合点“${rendezvous.place_name}”，状态最多保留 45 分钟。`
                          : `Current companions who can see this: ${groupPresence.members.filter((item) => !item.is_viewer).map((item) => item.display_name).join(", ")}. It is only about the shared-plan public stop “${rendezvous.place_name}” and expires within 45 minutes.`}
                      </p>
                      <p>
                        {zh
                          ? "不会读取或分享 GPS 起点、位置观察 ID 或个人路线；路线请求不会自动分享状态。此内容是未经验证的本人自报，不是路线危险、物理到场或 R69 会合确认。"
                          : "This does not read or share GPS origin, observation IDs, or private routes; a route request never shares status automatically. It is your unverified self-report, not evidence of route danger, physical arrival, or R69 reunion."}
                      </p>
                      <div className="journey-membership-actions">
                        <button
                          type="button"
                          data-testid="journey-rendezvous-consent-confirm"
                          disabled={coordinationBusy || coordinationConsentKey !== currentTargetKey || coordinationConsentAudienceKey !== currentAudienceKey}
                          onClick={() => void shareCoordinationSignal()}
                        >{coordinationConsentKind === "HEADING_TO_RENDEZVOUS"
                          ? (zh ? "确认分享我正在前往" : "Share that I'm on my way")
                          : (zh ? "确认请求帮助" : "Share my help request")}</button>
                        <button
                          type="button"
                          disabled={coordinationBusy}
                          onClick={() => {
                            setCoordinationConsentKind(null);
                            setCoordinationConsentKey(null);
                            setCoordinationConsentAudienceKey(null);
                          }}
                        >{zh ? "取消" : "Cancel"}</button>
                      </div>
                    </section>
                  )}
                </section>
              )}
            </>
          ) : (
            <small>{zh ? "暂时没有可确认的共同会合点（UNKNOWN / ROUTE_UNAVAILABLE）；没有读取位置或报告到达。" : "No shared rendezvous is currently available (UNKNOWN / ROUTE_UNAVAILABLE). No location was read and no arrival was reported."}</small>
          )}
        </div>
      )}

      {mapConsent && rendezvous && (
        <section className="journey-membership-map-consent" role="dialog" aria-modal="true" aria-labelledby="journey-membership-map-title" data-testid="journey-membership-map-consent">
          <strong id="journey-membership-map-title">{zh ? "打开公开会合点地图？" : "Open a public map of the rendezvous?"}</strong>
          <p>{hasExactPublicPin(rendezvous)
            ? (zh
              ? `同意后，会把共享计划中公共地点“${rendezvous.place_name}”的精确 WGS84 目的地坐标发送给 OpenStreetMap 标点。不会发送你的起点或位置，不会生成路线、ETA 或到达记录；这也不是队伍当前所在位置的证明。`
              : `If you agree, the shared public Place's exact WGS84 destination coordinates are sent to OpenStreetMap for a map pin. Your origin is not sent; no route, ETA or arrival record is created, and this does not prove where the group currently is.`)
            : (zh
              ? `同意后，只会向 OpenStreetMap 发送公开地点名称“${rendezvous.place_name}”进行搜索；结果可能不唯一。不会发送你的起点或位置，不会生成路线、ETA 或到达记录。`
              : `If you agree, only the public Place name “${rendezvous.place_name}” is sent to OpenStreetMap search; results may be ambiguous. Your origin is not sent; no route, ETA or arrival record is created.`)}</p>
          <div className="journey-membership-actions">
            <button type="button" data-testid="journey-membership-map-confirm" onClick={() => void openPublicMap()} disabled={navigationBusy}>
              {zh ? "同意并打开地图" : "Agree and open map"}
            </button>
            <button type="button" onClick={() => {
              setMapConsent(false);
              setMapConsentKey(null);
            }}>{zh ? "取消" : "Cancel"}</button>
          </div>
        </section>
      )}

      {buddyMapConsentHandoff && (
        <section
          className="journey-membership-map-consent"
          role="dialog"
          aria-modal="true"
          aria-labelledby="journey-buddy-map-title"
          data-testid="journey-buddy-map-consent"
        >
          <strong id="journey-buddy-map-title">{zh ? "打开双方同意的接应地点？" : "Open the mutually accepted handoff place?"}</strong>
          <p>{zh
            ? `继续前会重新向服务器验证双方同意和有效期。只有公开地点“${buddyMapConsentHandoff.handoff_place.name}”的 WGS84 坐标会发送给 OpenStreetMap；不会发送你的起点、成员位置或路线。`
            : `The server will revalidate mutual acceptance and expiry first. Only the public Place “${buddyMapConsentHandoff.handoff_place.name}” WGS84 coordinates go to OpenStreetMap; your origin, member locations, and routes are not sent.`}</p>
          <p>{zh
            ? "此地图只是公共地点标点，不是队伍位置、实时路线、可达性或物理到场证明。"
            : "This is only a public-place pin, not a group location, live route, reachability, or physical-arrival proof."}</p>
          <div className="journey-membership-actions">
            <button type="button" data-testid="journey-buddy-map-confirm" onClick={() => void openAcceptedBuddyMap()} disabled={buddyActionBusy}>
              {zh ? "同意并打开地图" : "Agree and open map"}
            </button>
            <button type="button" data-testid="journey-buddy-map-cancel" onClick={() => setBuddyMapConsentId(null)} disabled={buddyActionBusy}>
              {zh ? "取消" : "Cancel"}
            </button>
          </div>
        </section>
      )}

      {buddyDirectionsConsent && (
        <section
          className="journey-membership-map-consent"
          role="dialog"
          aria-modal="true"
          aria-labelledby="journey-buddy-directions-title"
          data-testid="journey-buddy-directions-consent"
        >
          <strong id="journey-buddy-directions-title">
            {zh ? "把双方同意的接应目的地交给外部步行导航？" : "Send the mutually agreed handoff destination to an external walking map?"}
          </strong>
          <p>{zh
            ? `只有公开地点“${buddyDirectionsConsent.handoff.handoff_place.name}”的 WGS84 目的地坐标会交给 Google Maps。链接不含你的保存起点、成员位置或路线；Google Maps 可能会按自己的权限流程读取设备位置。CityNexus 不会提供服务器计算的路线或 ETA。`
            : `Only the WGS84 destination of the public Place “${buddyDirectionsConsent.handoff.handoff_place.name}” goes to Google Maps. The link contains no stored origin, member location, or route; Google Maps may request device location under its own permission flow. CityNexus does not provide a server-calculated route or ETA.`}</p>
          <p>{zh
            ? "打开导航不代表你已开始导航、到达接应点或重新加入共同计划。若外部导航不可用，可改用卡片上另一个单独同意的静态地点标点。"
            : "Opening directions does not prove that navigation started, that you arrived, or that you rejoined the shared Journey. If external directions are unsupported, use the separate static-place-pin action on this card."}</p>
          <div className="journey-membership-actions">
            <button type="button" data-testid="journey-buddy-directions-confirm" onClick={() => void openAcceptedBuddyDirections()} disabled={buddyActionBusy}>
              {zh ? "同意并打开步行导航" : "Agree and open walking directions"}
            </button>
            <button type="button" data-testid="journey-buddy-directions-cancel" onClick={() => setBuddyDirectionsConsent(null)} disabled={buddyActionBusy}>
              {zh ? "取消，不发送目的地" : "Cancel without sending the destination"}
            </button>
          </div>
        </section>
      )}

      {buddyReturnDirectionsConsent && (
        <section
          className="journey-membership-map-consent"
          role="dialog"
          aria-modal="true"
          aria-labelledby="journey-buddy-return-directions-title"
          data-testid="journey-buddy-return-directions-consent"
        >
          <strong id="journey-buddy-return-directions-title">
            {zh ? "导航回当前共同会合点？" : "Navigate back to the current shared stop?"}
          </strong>
          <p>{zh
            ? `确认后只把当前共同会合点“${buddyReturnDirectionsConsent.sharedStopName}”的 WGS84 坐标作为目的地交给 Google Maps。CityNexus 不会提供或发送起点、成员位置、路线或 ETA；Google Maps 可能按自己的权限流程读取设备位置。`
            : `If you confirm, only the WGS84 destination of the current shared stop “${buddyReturnDirectionsConsent.sharedStopName}” goes to Google Maps. CityNexus does not provide or send an origin, member locations, route, or ETA; Google Maps may request device location under its own permission flow.`}</p>
          <p>{zh
            ? "打开外部导航不代表已重新加入 Journey、抵达或经过设备验证。若计划、游标或会合点变化，系统会阻止本次发送并要求重新确认。"
            : "Opening external directions does not prove that you rejoined the Journey, arrived, or were device-verified. If the plan, cursor, or rendezvous changes, this consent is blocked and must be renewed."}</p>
          <div className="journey-membership-actions">
            <button type="button" data-testid="journey-buddy-return-directions-confirm" onClick={() => void openBuddyReturnDirections()} disabled={buddyActionBusy}>
              {zh ? "单独同意并导航回共同会合点" : "Separately consent and navigate to the shared stop"}
            </button>
            <button type="button" data-testid="journey-buddy-return-directions-cancel" onClick={() => setBuddyReturnDirectionsConsent(null)} disabled={buddyActionBusy}>
              {zh ? "取消，不发送目的地" : "Cancel without sending the destination"}
            </button>
          </div>
        </section>
      )}

      {buddyRouteConsent && buddyRouteConsentHandoff && (
        <section
          className="journey-membership-map-consent"
          role="dialog"
          aria-modal="true"
          aria-labelledby="journey-buddy-route-title"
          data-testid="journey-buddy-route-consent"
          data-consent-step={buddyRouteConsent.step}
        >
          <strong id="journey-buddy-route-title">
            {buddyRouteConsent.step === "LOCATION"
              ? (zh ? "只为我自己记录一次前台位置？" : "Record one foreground location for my route?")
              : (zh ? "单独同意把我的起点发送给路线服务？" : "Separately allow sending my origin to a routing provider?")}
          </strong>
          {buddyRouteConsent.step === "LOCATION" ? (
            <>
              <p>{zh
                ? "继续会由设备前台读取一次位置，并将观察保留在这趟 Journey、标记为仅本人接应路线输入。它不会进入团队位置状态、启动位置或共享路线；另一位成员和旁观者 B 看不到精确起点或观察 ID。此步骤还不会调用路线服务。"
                : "Continuing reads one foreground device location and retains it in this Journey as caller-private handoff-route input. It is excluded from group presence, launch-origin, and shared-route projections; the other member and bystander B cannot see the precise origin or observation ID. This step does not call a routing provider."}</p>
              <div className="journey-membership-actions">
                <button type="button" data-testid="journey-buddy-route-location-confirm" onClick={() => void confirmBuddyRouteLocation()} disabled={buddyRouteBusy}>
                  {zh ? "同意并读取一次位置" : "Agree and read location once"}
                </button>
                <button type="button" data-testid="journey-buddy-route-cancel" onClick={cancelBuddyRouteConsent} disabled={buddyRouteBusy && buddyRouteConsent.step !== "LOCATION"}>
                  {zh ? "取消" : "Cancel"}
                </button>
              </div>
            </>
          ) : (
            <>
              <p>{zh
                ? `仅本人可见的位置观察已保存在 Journey 中。若已配置路线服务，继续会把你的精确起点发送一次，并只计算你到已接受接应点“${buddyRouteConsentHandoff.handoff_place.name}”的步行路线；另一位成员和旁观者 B 看不到你的路线。服务未配置、失败或证据不足时结果为 UNKNOWN，不会用模拟分钟数补齐。`
                : `Your caller-private location observation is retained in the Journey. If a routing provider is configured, continuing sends your precise origin once and calculates only your walking route to the accepted handoff “${buddyRouteConsentHandoff.handoff_place.name}”; the other member and bystander B cannot see your route. An unconfigured provider, failure, or insufficient evidence returns UNKNOWN, never mock minutes.`}</p>
              <p>{zh
                ? `目的地证据：${buddyRouteConsentHandoff.handoff_place.source_label}（${buddyRouteConsentHandoff.handoff_place.evidence_grade}）。原共同会合点“${buddyRouteConsentHandoff.original_shared_stop.place_name}”仍是全体行程地点；若接应点不同，接应点到原会合点的后续路线未知。只显示承诺风险，不修改计划、不自动触发 Guardian、不推断到场。公共地点地图仍需单独同意。`
                : `Destination evidence: ${buddyRouteConsentHandoff.handoff_place.source_label} (${buddyRouteConsentHandoff.handoff_place.evidence_grade}). The original shared stop “${buddyRouteConsentHandoff.original_shared_stop.place_name}” remains the whole Journey's stop; if the handoff differs, the onward route to that stop is unknown. This shows commitment risk only; it does not change the plan, trigger Guardian, or infer arrival. The public-place map has separate consent.`}</p>
              <div className="journey-membership-actions">
                <button type="button" data-testid="journey-buddy-route-provider-confirm" onClick={() => void confirmBuddyRouteProviderDisclosure()} disabled={buddyRouteBusy}>
                  {zh ? "单独同意并检查我的步行路线" : "Separately agree and check MY walking route"}
                </button>
                <button type="button" data-testid="journey-buddy-route-cancel" onClick={cancelBuddyRouteConsent} disabled={buddyRouteBusy}>
                  {zh ? "取消，不发送路线请求" : "Cancel without sending the route request"}
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {routeConsent && rendezvous && (
        <section
          className="journey-membership-map-consent"
          role="dialog"
          aria-modal="true"
          aria-labelledby="journey-membership-route-title"
          data-testid="journey-membership-route-consent"
        >
          <strong id="journey-membership-route-title">
            {zh ? "允许这次前台位置路线检查？" : "Allow this foreground route check?"}
          </strong>
          <p>{zh
            ? `继续会为这趟 Journey 记录一次由你主动发起的前台位置观察；该观察会保留，并可能以粗略同行状态供旅伴查看。你的精确起点可能发送给明确配置的路线服务。若现有路线风险规则判定需要，可能记录承诺风险并触发 Guardian。拒绝后仍可查看公开会合点地图。不会后台采样、持续追踪、静默修改共享计划，也不会把路线结果当作已到达或已与大家会合。`
            : `Continuing records a Journey-bound foreground location observation that you explicitly request; it is retained and may affect the coarse group-presence state companions can see. Your precise origin may be sent to the configured routing provider. Existing route-risk rules may record a protected-commitment risk and trigger Guardian. Declining keeps the public meeting-place map available. There is no background sampling or tracking, the shared plan is not silently changed, and a route result is not an arrival or proof that the group is together.`}</p>
          <p>{zh
            ? `目的地是当前共享计划中的公共地点：${rendezvous.place_name}。`
            : `Destination: ${rendezvous.place_name}, a public Place in the current shared plan.`}</p>
          <div className="journey-membership-actions">
            <button
              type="button"
              data-testid="journey-membership-route-confirm"
              onClick={() => void requestPersonalRoute()}
              disabled={navigationBusy}
            >{zh ? "同意并检查路线" : "Agree and check route"}</button>
            <button
              type="button"
              data-testid="journey-membership-route-cancel"
              onClick={() => {
                setRouteConsent(false);
                setRouteConsentKey(null);
              }}
              disabled={navigationBusy}
            >{zh ? "取消，保留公开地图" : "Cancel and keep public map"}</button>
          </div>
        </section>
      )}

      {visibleRoute && (
        <article
          className="journey-membership-catchup"
          data-testid="journey-membership-route-result"
          data-evidence={visibleRouteClass ?? "UNKNOWN"}
          data-provenance={visibleRoute.route.provenance ?? "UNKNOWN"}
        >
          <strong>{visibleRouteClass === "LIVE"
            ? (zh ? "当前路线服务结果" : "Current route-provider result")
            : visibleRouteClass === "REPLAY"
              ? (zh ? "历史路线回放（不是当前路况）" : "Captured historical route (not current traffic)")
              : (zh ? "路线状态：UNKNOWN / ROUTE_UNAVAILABLE" : "Route status: UNKNOWN / ROUTE_UNAVAILABLE")}</strong>
          <p>{zh
            ? `共享计划目的地：${visibleRoute.target.place_name}。这表示路线目的地，不表示队伍当前就在此处。`
            : `Shared-plan destination: ${visibleRoute.target.place_name}. This identifies the planned stop, not where the group is physically located.`}</p>
          {visibleRouteClass === "UNKNOWN" ? (
            <small>{zh ? "没有显示预计分钟数；请使用公开地点地图或手动联系旅伴。" : "No duration is shown. Use the public-place map or coordinate with companions manually."}</small>
          ) : (
            <small>{zh
              ? `步行约 ${visibleRoute.route.duration_min} 分钟。`
              : `Walking estimate: about ${visibleRoute.route.duration_min} minutes.`}</small>
          )}
          <small>
            {zh ? "来源：" : "Source: "}{visibleRoute.route.provider ?? (zh ? "未知" : "unknown")}
            {" · "}{zh ? "证据：" : "Evidence: "}{visibleRoute.route.provenance ?? "UNKNOWN"}
            {visibleRoute.route.captured_at && (
              <>{" · "}{zh ? "路线回执时间：" : "Route receipt time: "}{new Date(visibleRoute.route.captured_at).toLocaleString(locale)}</>
            )}
          </small>
          <small>
            {zh ? "定位依据：" : "Location authority: "}{visibleRoute.authority}
          </small>
          {visibleRouteClass !== "UNKNOWN" && visibleRoute.shared_commitment_risk && (
            <small>
              {zh ? "下一项受保护承诺风险：" : "Next protected-commitment risk: "}
              {visibleRoute.shared_commitment_risk.state}
              {typeof visibleRoute.shared_commitment_risk.slack_after_buffer_min === "number"
                ? ` · ${zh ? "扣除缓冲后的余量" : "slack after buffer"}: ${visibleRoute.shared_commitment_risk.slack_after_buffer_min} min`
                : ""}
            </small>
          )}
          <small>
            {zh ? "Guardian 服务器回执：" : "Guardian server receipt: "}
            {visibleRoute.guardian_triggered ? "triggered" : "not triggered"}
            {" · "}{zh ? "风险事件已记录：" : "risk event recorded: "}
            {visibleRoute.member_risk_event_id ? (zh ? "是" : "yes") : (zh ? "否" : "no")}
            {" · "}{zh ? "处理状态：" : "status: "}
            {String(visibleRoute.guardian?.status ?? "NO_HANDOFF_STATUS_RETURNED")}
          </small>
        </article>
      )}

      {message && <small role="status" data-testid="journey-membership-message">{message}</small>}
    </section>
  );
}
